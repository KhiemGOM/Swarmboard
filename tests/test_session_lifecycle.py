import concurrent.futures
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from board.core import Hub, BoardError
from board.mcp import native_registration


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hub = Hub(self.root/'hub.sqlite', self.root)
        self.registration = {'name':'codex', 'native_harness':'codex',
                             'native_session_key':hashlib.sha256(b'codex:chat-one').hexdigest()}
        self.agent = self.hub.call('register', self.registration)
        self.peer = self.hub.call('register', {'name':'peer'})

    def test_repeated_startup_keeps_one_id_and_old_bridge_calls_usable(self):
        claim = self.hub.call('claim', {**self.agent,'paths':['work.py'],'reason':'continue'})
        msg = self.hub.call('inbox_send', {**self.peer,'recipient_agent_id':self.agent['agent_id'],'body':'pending'})
        previous = self.agent
        for _ in range(4):
            current = self.hub.call('register', self.registration)
            self.assertEqual(current['agent_id'], self.agent['agent_id'])
            self.hub.call('board_status', previous)
            previous = current
        status = self.hub.call('board_status', {**current,'target_agent_id':'self'})
        self.assertEqual([r['id'] for r in status['claims']], claim['claim_ids'])
        self.assertEqual(self.hub.call('inbox_read', current)['messages'][0]['id'], msg['id'])
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agents').fetchone()[0], 2)

    def test_concurrent_startup_cannot_mint_multiple_native_ids(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            identities = list(pool.map(lambda _: self.hub.call('register', self.registration), range(8)))
        self.assertEqual({r['agent_id'] for r in identities}, {self.agent['agent_id']})
        with self.hub.connect() as db:
            live_token = db.execute('SELECT token FROM agents WHERE id=?', (self.agent['agent_id'],)).fetchone()[0]
        self.assertEqual(sum(r['session_token']==live_token for r in identities), 1)

    def test_retired_chat_reconnects_with_same_id_without_restoring_ownership(self):
        claim = self.hub.call('claim', {**self.agent, 'paths':['work.py'], 'reason':'work'})
        job = self.hub.call('job_post', {**self.agent, 'title':'work', 'description':'work', 'acceptance':'done'})['job']
        self.hub.call('job_claim', {**self.agent, 'job_id':job['id']})
        self.hub.call('retire', {**self.peer,'target_agent_id':self.agent['agent_id'],'reason':'owner retired'})
        self.assert_send_rejected('retired')
        resumed = self.hub.call('register', self.registration)
        self.assertEqual(resumed['agent_id'], self.agent['agent_id'])
        self.assertNotEqual(resumed['session_token'], self.agent['session_token'])
        status = self.hub.call('board_status', {**resumed,'target_agent_id':'self'})
        self.assertEqual(status['claims'], [])
        self.assertEqual(status['custody'][0]['state'], 'HELD')
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM claims WHERE id=?', (claim['claim_ids'][0],)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 'OPEN')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agents').fetchone()[0], 2)
        self.hub.call('board_status', self.agent)

    def assert_send_rejected(self, error):
        with self.hub.connect() as db:
            before = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        with self.assertRaisesRegex(BoardError, error):
            self.hub.call('inbox_send', {**self.peer,'recipient_agent_id':self.agent['agent_id'],'body':'must not arrive'})
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], before)

    def test_disconnected_send_rejected_then_authenticated_reconnect_restores(self):
        self.hub.call('disconnect', {**self.agent,'reason':'bridge closed'})
        self.assert_send_rejected('disconnected')
        current = self.hub.call('register', self.registration)
        self.hub.call('inbox_send', {**self.peer,'recipient_agent_id':current['agent_id'],'body':'live again'})

    def test_inheritance_invalidates_old_credentials_but_native_owner_can_rejoin(self):
        claim = self.hub.call('claim', {**self.agent, 'paths':['handed.py'], 'reason':'work'})
        self.hub.call('inherit', {**self.peer,'target_agent_id':self.agent['agent_id'],
                                 'inheritance_secret':self.agent['inheritance_secret'],'reason':'continue'})
        for method in ('board_status','inbox_read','inbox_ack','claim','inheritance_key','disconnect'):
            with self.subTest(method=method), self.assertRaisesRegex(BoardError, 'retired'):
                self.hub.call(method, self.agent)
        self.assert_send_rejected('Address successor')
        resumed = self.hub.call('register', self.registration)
        self.assertEqual(resumed['agent_id'], self.agent['agent_id'])
        self.assertEqual(self.hub.call('board_status', {**resumed,'target_agent_id':'self'})['claims'], [])
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT agent FROM claims WHERE id=?', (claim['claim_ids'][0],)).fetchone()[0], self.peer['agent_id'])
        self.hub.call('board_status', self.agent)

    def test_retirement_and_inheritance_disable_old_wake_binding(self):
        self.hub.call('board_desktop_wake', {**self.agent,'action':'enable','thread_id':'chat-one',
                                            'workspace':str(self.root),'reason':'native binding'})
        self.hub.call('inherit', {**self.peer,'target_agent_id':self.agent['agent_id'],
                                 'inheritance_secret':self.agent['inheritance_secret'],'reason':'continue'})
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT enabled FROM desktop_wake_bindings WHERE agent=?', (self.agent['agent_id'],)).fetchone()[0], 0)

    def test_unique_random_id_retries_collision(self):
        from board import core
        original = core.secrets.token_hex
        with patch('board.core.secrets.token_hex', side_effect=[self.agent['agent_id'], original(8)]):
            other = self.hub.call('register', {'name':'other'})
        self.assertNotEqual(other['agent_id'], self.agent['agent_id'])

    def test_legacy_migration_prefers_verified_wake_recipient_over_stale_hook(self):
        from board.hooks import owns_inbox_binding
        self.hub.call('board_desktop_wake', {**self.agent,'action':'enable','thread_id':'chat-one',
                                            'workspace':str(self.root),'reason':'verified chat'})
        key = self.registration['native_session_key']
        with self.hub.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS hook_inbox_bindings (harness TEXT,agent TEXT,session_key TEXT,PRIMARY KEY(harness,agent),UNIQUE(harness,session_key))')
            db.execute('INSERT INTO hook_inbox_bindings VALUES (?,?,?)', ('codex',self.peer['agent_id'],key))
            db.execute('DELETE FROM native_sessions')
        restored = Hub(self.hub.database,self.root)
        identity = restored.call('register',self.registration)
        self.assertEqual(identity['agent_id'],self.agent['agent_id'])

    def test_environment_binding_uses_native_id_and_does_not_guess(self):
        value = native_registration('codex', {'CODEX_THREAD_ID':'01a1273f-8677-71b0-880b-b6f5c4592637'})
        self.assertEqual(value['native_harness'], 'codex')
        self.assertEqual(native_registration('codex', {}), {'name':'codex'})
        self.assertEqual(native_registration('codex', {'CODEX_THREAD_ID':'bad\nid'}), {'name':'codex'})
        self.assertEqual(native_registration('claude', {'CODEX_THREAD_ID':'01a1273f-8677-71b0-880b-b6f5c4592637'}), {'name':'claude'})

    def test_parent_session_environment_cannot_collapse_forked_threads(self):
        parent = {'CODEX_SESSION_ID':'parent-session'}
        self.assertEqual(native_registration('codex',parent), {'name':'codex'})
        a = native_registration('codex',{**parent,'CODEX_THREAD_ID':'thr_parent'})
        b = native_registration('codex',{**parent,'CODEX_THREAD_ID':'thr_fork'})
        self.assertNotEqual(a['native_session_key'],b['native_session_key'])
        self.assertNotEqual(self.hub.call('register',a)['agent_id'],self.hub.call('register',b)['agent_id'])

    def test_name_persists_on_resume_and_does_not_change_identity(self):
        self.assertIn('name_reminder',self.hub.call('board_status',self.agent))
        result = self.hub.call('board_agent_name',{**self.agent,'name':'Maple - UI'})
        self.assertEqual(result['agent_id'],self.agent['agent_id'])
        self.assertNotIn('name_reminder',result)
        resumed = self.hub.call('register',self.registration)
        status = self.hub.call('board_status',{**resumed,'target_agent_id':'self'})
        self.assertEqual(status['your_name'],'Maple - UI')
        self.assertEqual(status['agents'][0]['name'],'Maple - UI')
        self.assertNotIn('name_reminder',status)

    def test_name_validation_unique_and_self_only(self):
        self.hub.call('board_agent_name',{**self.agent,'name':'Maple - UI'})
        for name in ('','   ','x'*65,'bad\nname','invisible\u200bname'):
            with self.subTest(name=name),self.assertRaisesRegex(BoardError,'readable'):
                self.hub.call('board_agent_name',{**self.peer,'name':name})
        with self.assertRaisesRegex(BoardError,'another active agent'):
            self.hub.call('board_agent_name',{**self.peer,'name':'maple - ui'})
        with self.assertRaisesRegex(BoardError,'only your own'):
            self.hub.call('board_agent_name',{**self.peer,'name':'Finch','target_agent_id':self.agent['agent_id']})
        self.assertEqual(self.hub.call('board_status',self.agent)['your_name'],'Maple - UI')

    def test_late_session_end_old_connection_cannot_disconnect_resumed_chat(self):
        resumed = self.hub.call('register',self.registration)
        with self.assertRaisesRegex(BoardError,'invalid session token'):
            self.hub.call('session_end',{**self.agent,'reason':'old native exit'})
        with self.hub.connect() as db:
            self.assertIsNone(db.execute('SELECT disconnected FROM agents WHERE id=?',(resumed['agent_id'],)).fetchone()[0])
        self.hub.call('session_end',{**resumed,'reason':'native exit'})
        self.assert_send_rejected('disconnected')
        repeated = self.hub.call('session_end',{**resumed,'reason':'native exit again'})
        self.assertTrue(repeated['already_disconnected'])

    def test_stdio_restarts_reuse_native_chat_and_inbox(self):
        import json
        import os
        import subprocess
        import sys
        import threading
        from board.server import serve
        server = serve(self.hub, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        env = {**os.environ, 'CODEX_THREAD_ID':'01a1273f-8677-71b0-880b-b6f5c4592637'}
        entry = Path(__file__).resolve().parents[1]/'run_board.py'
        ids = []
        message = None
        for _ in range(3):
            proc = subprocess.Popen([sys.executable,str(entry),'mcp','--name','codex',
                '--workspace',str(self.root),'--database',self.hub.database,
                '--url',f'http://127.0.0.1:{server.server_port}'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
            try:
                for n, method, params in [(1,'initialize',{'protocolVersion':'2025-11-25'}),
                    (2,'tools/call',{'name':'board_status','arguments':{'target_agent_id':'self','include_rules':False}}),
                    (3,'tools/call',{'name':'inbox_read','arguments':{}})]:
                    proc.stdin.write(json.dumps({'jsonrpc':'2.0','id':n,'method':method,'params':params})+'\n')
                    proc.stdin.flush()
                    reply = json.loads(proc.stdout.readline())
                    self.assertNotIn('error', reply)
                    if method == 'tools/call':
                        self.assertFalse(reply['result']['isError'], reply)
                        output = json.loads(reply['result']['content'][0]['text'])
                        if n == 2:
                            ids.append(output['your_agent_id'])
                            if message is None:
                                message = self.hub.call('inbox_send', {**self.peer,'recipient_agent_id':ids[-1],'body':'persist across reconnect'})['id']
                        else:
                            self.assertEqual(output['messages'][0]['id'], message)
            finally:
                proc.stdin.close()
                proc.wait(timeout=5)
                proc.stdout.close()
                proc.stderr.close()
        self.assertEqual(len(set(ids)), 1)
