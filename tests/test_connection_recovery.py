import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import swarmboard_hook
from board.core import BoardError, Hub
from board.hooks import Adapter
from board.runtime import workspace_connection
from types import SimpleNamespace


class ConnectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'hub.sqlite'
        self.hub = Hub(self.db, self.root)
        self.agent = self.hub.call('register', {'name':'Claude'})
        self.caller = lambda url, method, params: self.hub.call(method, params)
        self.adapter = Adapter(self.root, self.db, 'claude', caller=self.caller)

    def event(self, event, **params):
        return self.adapter.handle({'cwd':str(self.root),'session_id':'own-chat','hook_event_name':event,**params})

    def bind(self, error=None):
        return self.event('PostToolUse', tool_name='mcp__board__board_status',
            tool_response={'your_agent_id':self.agent['agent_id'], **({'error':error,'isError':True} if error else {})})

    def test_unconnected_edits_and_start_commands_are_allowed_connected_claims_are_checked(self):
        for name, arguments in [('Edit',{'file_path':str(self.root/'a.py')}), ('Bash',{'command':'python boardctl.py start'})]:
            self.assertEqual(self.event('PreToolUse',tool_name=name,tool_input=arguments), {})
        self.bind()
        denied = self.event('PreToolUse',tool_name='Edit',tool_input={'file_path':str(self.root/'a.py')})
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_mcp_disconnect_does_not_get_undone_by_a_hook_call(self):
        self.bind()
        self.hub.call('disconnect',{**self.agent,'reason':'Bridge exited'})
        self.assertEqual(self.event('PreToolUse',tool_name='Edit',tool_input={'file_path':'a.py'}), {})
        with self.hub.connect() as db:
            self.assertIsNotNone(db.execute('SELECT disconnected FROM agents WHERE id=?',(self.agent['agent_id'],)).fetchone()[0])

    def test_hook_failures_never_deny_and_only_notify_once(self):
        self.bind()
        payload={'cwd':str(self.root),'session_id':'own-chat','hook_event_name':'PreToolUse','tool_name':'Bash','tool_input':{'command':'python boardctl.py start'}}
        for error in (BoardError('Hub unavailable'), sqlite_error()):
            with patch('sys.argv',['swarmboard_hook.py','--harness','claude']), patch('sys.stdin',io.StringIO(json.dumps(payload))), patch('swarmboard_hook.hook_connection',return_value=(str(self.root),str(self.db),'http://127.0.0.1:1')), patch.object(Adapter,'handle',side_effect=error), redirect_stdout(io.StringIO()) as output:
                swarmboard_hook.main()
            result=json.loads(output.getvalue())
            self.assertNotIn('permissionDecision',result.get('hookSpecificOutput',{}))
        self.assertEqual(result,{})

    def test_id_recovery_needs_no_native_proof(self):
        self.bind()
        with self.hub.connect() as db:
            db.execute('UPDATE agents SET token=? WHERE id=?',('current-secret',self.agent['agent_id']))
        resumed = self.hub.call('native_reconnect', {'target_agent_id': self.agent['agent_id']})
        self.assertEqual(resumed['agent_id'], self.agent['agent_id'])
        self.hub.call('board_status', {'agent_id': resumed['agent_id']})

    def test_hot_journal_is_recovered_before_workspace_validation(self):
        config=self.root/'.codex'/'board-operator.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'workspace':str(self.root),'database':str(self.db),'port':4317}))
        code="import sqlite3,sys,os; db=sqlite3.connect(sys.argv[1]); db.execute('PRAGMA cache_size=1'); db.execute('BEGIN IMMEDIATE'); db.execute('UPDATE agents SET name=?',('x'*30000,)); os._exit(0)"
        subprocess.run([sys.executable,'-c',code,str(self.db)],check=True)
        result=workspace_connection(self.root)
        self.assertEqual(result[1],str(self.db))
        with self.hub.connect() as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT name FROM agents').fetchone()[0],'Claude')


def sqlite_error():
    import sqlite3
    return sqlite3.OperationalError('attempt to write a readonly database')
