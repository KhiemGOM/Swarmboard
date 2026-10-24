"""0.12 routing redesign: identity and board come from the calling chat, never from bridge-global state.

Plan and potential failures: docs/routing-redesign.md. Covered here:
- Two chats through ONE bridge process (Claude Desktop shared bridge) must get two identities and each
  its own project board; the same chat through two bridges keeps one identity.
- Routing never guesses from the bridge's own cwd: no route file means WORKSPACE_REQUIRED, and nothing
  is created at an unconfigured folder such as C:\\.
- A Claude bridge with no native session and no hook-injected route refuses instead of sharing one ID.
- One canonical route file; stale (workspace-mismatched) files are ignored; legacy Codex routes still
  resolve and /board migrates them; /board cleans the reconnect cache and old hook state.
- The hook stamps every board call with the chat's route, overwriting model-supplied values.
- /board reset is the only way to wipe a board, gated on the user's own typed command, used once.
- History retention prunes old finished data but never live work.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from board.core import BoardError, Hub
from board.hooks import Adapter
from board.server import serve

ROOT = Path(__file__).resolve().parents[1]
CLEAN_ENV = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID')}


def native_key(harness, session):
    return hashlib.sha256(f'{harness}:{session}'.encode()).hexdigest()


def write_route(directory, database, port, workspace=None, legacy=False):
    path = Path(directory) / ('.codex/board-operator.json' if legacy else '.board/operator.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'workspace': str(workspace or directory), 'database': str(database), 'port': port}), encoding='utf-8')
    return path


class Project:
    """A real project folder with its own running hub and canonical route file."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.hub = Hub(self.root / '.board/hub.sqlite', self.root)
        self.server = serve(self.hub, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        write_route(self.root, self.hub.database, self.server.server_port)

    def agents(self):
        with self.hub.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,state FROM agents ORDER BY started')]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Bridge:
    """One long-lived stdio bridge process, like the one Claude Desktop shares between chats."""

    def __init__(self, cwd, name='claude-code', env=None, extra=()):
        self.process = subprocess.Popen([sys.executable, str(ROOT / 'run_board.py'), 'mcp', '--name', name, *extra],
                                        cwd=cwd, env=env or CLEAN_ENV, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding='utf-8')
        self.next_id = 0
        self.request('initialize', {'protocolVersion': '2025-11-25'})

    def request(self, method, params=None):
        self.next_id += 1
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.next_id, 'method': method, 'params': params or {}}) + '\n')
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())

    def tool(self, name, arguments=None, route=None):
        arguments = dict(arguments or {})
        if route is not None:
            arguments['_route'] = route
        reply = self.request('tools/call', {'name': name, 'arguments': arguments})
        result = reply['result']
        return result['isError'], json.loads(result['content'][0]['text'])

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=20)
        self.process.stdout.close()
        self.process.stderr.close()


def route(session, cwd, harness='claude'):
    return {'harness': harness, 'session_id': session, 'cwd': str(cwd)}


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.projects = []
        self.bridges = []

    def tearDown(self):
        for bridge in self.bridges:
            bridge.close()
        for project in self.projects:
            project.close()
        self.temp.cleanup()

    def project(self, name):
        project = Project(self.base / name)
        self.projects.append(project)
        return project

    def bridge(self, cwd=None, **kwargs):
        bridge = Bridge(cwd or self.base, **kwargs)
        self.bridges.append(bridge)
        return bridge


class SharedBridgeTests(BridgeCase):
    # --- basics ---

    def test_two_chats_through_one_bridge_get_their_own_identity_and_board(self):
        fire, notice = self.project('FireCracking'), self.project('AgentsNoticeBoard')
        shared = self.bridge(cwd=self.base)  # Desktop launches it outside every project.
        error, first = shared.tool('board_status', {'include_rules': False}, route('chat-fire', fire.root))
        self.assertFalse(error, first)
        error, second = shared.tool('board_status', {'include_rules': False}, route('chat-notice', notice.root / 'board'))
        self.assertFalse(error, second)
        self.assertNotEqual(first['your_agent_id'], second['your_agent_id'])
        self.assertEqual([a['id'] for a in fire.agents()], [first['your_agent_id']])
        self.assertEqual([a['id'] for a in notice.agents()], [second['your_agent_id']])
        # Interleaving the chats never swaps identities.
        self.assertEqual(shared.tool('board_info', {}, route('chat-fire', fire.root))[1]['your_agent_id'], first['your_agent_id'])
        self.assertEqual(shared.tool('board_info', {}, route('chat-notice', notice.root))[1]['your_agent_id'], second['your_agent_id'])

    def test_same_chat_through_two_bridges_keeps_one_identity(self):
        project = self.project('repo')
        one, two = self.bridge(cwd=project.root), self.bridge(cwd=project.root)
        first = one.tool('board_info', {}, route('chat', project.root))[1]['your_agent_id']
        second = two.tool('board_info', {}, route('chat', project.root))[1]['your_agent_id']
        self.assertEqual(first, second)
        self.assertEqual(len(project.agents()), 1)

    def test_identity_is_bound_to_the_native_chat_on_the_hub(self):
        project = self.project('repo')
        agent = self.bridge().tool('board_info', {}, route('chat', project.root))[1]['your_agent_id']
        with project.hub.connect() as db:
            row = db.execute('SELECT agent FROM native_sessions WHERE harness=? AND session_key=?',
                             ('claude', native_key('claude', 'chat'))).fetchone()
        self.assertEqual(row[0], agent)

    def test_every_tool_schema_declares_the_route_field(self):
        tools = self.bridge().request('tools/list')['result']['tools']
        self.assertTrue(all('_route' in t['inputSchema']['properties'] for t in tools))

    def test_codex_bridge_without_hooks_uses_its_own_thread_and_cwd(self):
        project = self.project('repo')
        env = {**CLEAN_ENV, 'CODEX_THREAD_ID': 'thread-1'}
        error, output = self.bridge(cwd=project.root / '.board', name='codex', env=env).tool('board_info')
        self.assertFalse(error, output)
        with project.hub.connect() as db:
            row = db.execute('SELECT agent FROM native_sessions WHERE harness=? AND session_key=?',
                             ('codex', native_key('codex', 'thread-1'))).fetchone()
        self.assertEqual(row[0], output['your_agent_id'])

    # --- potential failures ---

    def test_unconfigured_folder_gets_workspace_required_and_no_board_is_created(self):
        plain = self.base / 'plain'
        plain.mkdir()
        error, output = self.bridge(cwd=plain).tool('board_status', {}, route('chat', plain))
        self.assertTrue(error)
        self.assertIn('WORKSPACE_REQUIRED', output['error'])
        self.assertFalse((plain / '.board').exists())

    def test_claude_bridge_without_native_session_or_route_refuses(self):
        project = self.project('repo')
        error, output = self.bridge(cwd=project.root).tool('board_status')
        self.assertTrue(error)
        self.assertIn('claude_desktop_config.json', output['error'])
        self.assertEqual(project.agents(), [])

    def test_stale_route_file_does_not_hijack_routing(self):
        project = self.project('repo')
        other = self.base / 'SkillScoped'
        other.mkdir()
        # A copied route naming a different workspace sits in a subfolder of the project.
        write_route(project.root / 'sub', project.root / 'elsewhere.sqlite', 1, workspace=other)
        error, output = self.bridge().tool('board_info', {}, route('chat', project.root / 'sub'))
        self.assertFalse(error, output)
        self.assertEqual(Path(output['workspace']), project.root)

    def test_agent_removed_by_reset_is_reregistered_transparently(self):
        project = self.project('repo')
        bridge = self.bridge()
        first = bridge.tool('board_info', {}, route('chat', project.root))[1]['your_agent_id']
        project.hub.call('board_reset', {'agent_id': first, 'reason': 'test wipe'})
        error, output = bridge.tool('board_info', {}, route('chat', project.root))
        self.assertFalse(error, output)
        self.assertEqual([a['id'] for a in project.agents()], [output['your_agent_id']])


class RouteResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_nearest_valid_route_is_found_from_a_subfolder(self):
        from board.runtime import find_project
        write_route(self.base, self.base / '.board/hub.sqlite', 4400)
        (self.base / 'a/b').mkdir(parents=True)
        self.assertEqual(find_project(self.base / 'a/b'), self.base)

    def test_canonical_route_wins_over_legacy_and_stale_canonical_is_ignored(self):
        from board.runtime import find_project, read_route
        write_route(self.base, self.base / 'legacy.sqlite', 4401, legacy=True)
        write_route(self.base, self.base / 'x.sqlite', 4317, workspace=self.base / 'Other')
        self.assertIsNone(read_route(self.base, legacy=False))
        self.assertEqual(read_route(self.base)['port'], 4401)  # Stale canonical falls back to the valid legacy one.
        write_route(self.base, self.base / 'canon.sqlite', 4402)
        self.assertEqual(read_route(self.base)['port'], 4402)
        self.assertEqual(find_project(self.base), self.base)

    def test_no_route_anywhere_is_none_and_connection_refuses(self):
        from board.runtime import find_project, workspace_connection
        self.assertIsNone(find_project(self.base))
        old = Path.cwd()
        os.chdir(self.base)
        try:
            with self.assertRaisesRegex(BoardError, 'WORKSPACE_REQUIRED'):
                workspace_connection()
        finally:
            os.chdir(old)
        self.assertFalse((self.base / '.board').exists())

    def test_prepare_route_creates_default_at_git_root(self):
        from board.runtime import prepare_route
        (self.base / '.git').mkdir()
        (self.base / 'src').mkdir()
        workspace, database, url = prepare_route(self.base / 'src')[:3]
        self.assertEqual(Path(workspace), self.base)
        self.assertEqual(Path(database), self.base / '.board/hub.sqlite')
        saved = json.loads((self.base / '.board/operator.json').read_text())
        self.assertEqual(Path(saved['workspace']), self.base)
        self.assertEqual(url, f"http://127.0.0.1:{saved['port']}")
        self.assertEqual(prepare_route(self.base / 'src')[:3], (workspace, database, url))  # Idempotent.

    def test_prepare_route_migrates_legacy_and_replaces_stale_files(self):
        from board.runtime import prepare_route
        legacy = write_route(self.base, self.base / '.board/development.sqlite', 4318, legacy=True)
        write_route(self.base, self.base / '.board/skillscoped/hub.sqlite', 4317, workspace=self.base / 'SkillScoped')
        workspace, database, url, cleaned = prepare_route(self.base)
        self.assertEqual((Path(database), url), (self.base / '.board/development.sqlite', 'http://127.0.0.1:4318'))
        self.assertFalse(legacy.exists())
        self.assertEqual(Path(json.loads((self.base / '.board/operator.json').read_text())['workspace']), self.base)
        self.assertTrue(cleaned)

    def test_prepare_route_clears_reconnect_cache_and_old_hook_state_only(self):
        from board.runtime import prepare_route
        write_route(self.base, self.base / '.board/hub.sqlite', 4403)
        cache = self.base / '.board/native-reconnect/abc.json'
        cache.parent.mkdir(parents=True)
        cache.write_text('{}')
        sessions = self.base / '.board/hook-sessions'
        sessions.mkdir()
        old, recent = sessions / ('a' * 64 + '.json'), sessions / ('b' * 64 + '.json')
        old.write_text('{}')
        recent.write_text('{"activated": true}')
        stale = time.time() - 30 * 86400
        os.utime(old, (stale, stale))
        prepare_route(self.base)
        self.assertFalse(cache.parent.exists())
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())


class HookRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / '.board/hub.sqlite'
        self.hub = Hub(self.db, self.root)
        self.adapter = Adapter(self.root, self.db, 'claude', caller=lambda url, method, params: self.hub.call(method, params))

    def tearDown(self):
        self.temp.cleanup()

    def event(self, event, session='chat', **params):
        return self.adapter.handle({'session_id': session, 'cwd': str(self.root), 'hook_event_name': event, **params})

    def activate(self, session='chat', prompt='/board'):
        return self.event('UserPromptSubmit', session, prompt=prompt)

    def native_agent(self, session='chat'):
        return self.hub.call('register', {'name': 'claude-code', 'native_harness': 'claude',
                                          'native_session_key': native_key('claude', session)})

    def decision(self, reply):
        return reply.get('hookSpecificOutput', {}).get('permissionDecision')

    # --- basics ---

    def test_board_calls_are_stamped_with_this_chats_route(self):
        self.activate()
        reply = self.event('PreToolUse', tool_name='mcp__board__claim', tool_input={'paths': ['a.py'], 'reason': 'r'})
        output = reply['hookSpecificOutput']
        self.assertNotEqual(output.get('permissionDecision'), 'deny')
        self.assertEqual(output['updatedInput'], {'paths': ['a.py'], 'reason': 'r',
                         '_route': {'harness': 'claude', 'session_id': 'chat', 'cwd': str(self.root)}})

    def test_model_supplied_route_is_overwritten(self):
        self.activate()
        forged = {'harness': 'claude', 'session_id': 'someone-else', 'cwd': 'C:\\'}
        reply = self.event('PreToolUse', tool_name='mcp__board__board_status', tool_input={'_route': forged})
        self.assertEqual(reply['hookSpecificOutput']['updatedInput']['_route']['session_id'], 'chat')

    def test_hook_identity_comes_from_the_native_binding_without_any_response_scraping(self):
        self.native_agent()
        self.activate()
        self.assertEqual(self.decision(self.event('PreToolUse', tool_name='Write', tool_input={'file_path': 'a.py'})), 'deny')

    def test_unbound_chat_is_not_gated(self):
        self.activate()
        self.assertIsNone(self.decision(self.event('PreToolUse', tool_name='Write', tool_input={'file_path': 'a.py'})))

    # --- potential failures ---

    def test_response_from_a_different_agent_does_not_relink_a_bound_chat(self):
        mine = self.native_agent()
        other = self.hub.call('register', {'name': 'shared-bridge'})
        self.activate()
        reply = self.event('PostToolUse', tool_name='mcp__board__board_status',
                           tool_response={'content': [{'type': 'text', 'text': json.dumps({'your_agent_id': other['agent_id']})}]})
        self.assertIn('claude_desktop_config.json', reply['hookSpecificOutput']['additionalContext'])
        with self.hub.connect() as db:
            bound = db.execute('SELECT agent FROM native_sessions WHERE session_key=?', (native_key('claude', 'chat'),)).fetchone()[0]
        self.assertEqual(bound, mine['agent_id'])

    def test_reset_tool_requires_the_users_own_reset_command_once(self):
        self.activate()
        reset = lambda: self.event('PreToolUse', tool_name='mcp__board__board_reset', tool_input={'reason': 'r'})
        self.assertEqual(self.decision(reset()), 'deny')  # Plain /board does not authorize a wipe.
        self.activate(prompt='/board reset')
        self.assertNotEqual(self.decision(reset()), 'deny')
        self.assertEqual(self.decision(reset()), 'deny')  # Consumed.
        self.activate(prompt='/board reset')
        self.event('UserPromptSubmit', prompt='actually never mind')
        self.assertEqual(self.decision(reset()), 'deny')  # Only the prompt that asked for it.

    def test_reset_command_in_another_chat_does_not_authorize_this_one(self):
        self.activate()
        self.activate('other', '/board reset')
        reply = self.event('PreToolUse', tool_name='mcp__board__board_reset', tool_input={'reason': 'r'})
        self.assertEqual(self.decision(reply), 'deny')


class HookEntryTests(unittest.TestCase):
    """swarmboard_hook.py end to end: /board sets up routing in a fresh project."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / '.git').mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_hook(self, **payload):
        payload = {'session_id': 'chat', 'cwd': str(self.root), **payload}
        run = subprocess.run([sys.executable, str(ROOT / 'swarmboard_hook.py'), '--harness', 'claude'],
                             input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8',
                             env={**CLEAN_ENV, 'USERPROFILE': str(self.root), 'HOME': str(self.root)}, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_plain_prompt_in_unconfigured_project_creates_nothing(self):
        self.assertEqual(self.run_hook(hook_event_name='UserPromptSubmit', prompt='hello'), {})
        self.assertFalse((self.root / '.board').exists())

    def test_board_prompt_creates_route_and_activates(self):
        reply = self.run_hook(hook_event_name='UserPromptSubmit', prompt='/board')
        self.assertIn('activated', reply['hookSpecificOutput']['additionalContext'])
        self.assertTrue((self.root / '.board/operator.json').is_file())
        stamped = self.run_hook(hook_event_name='PreToolUse', tool_name='mcp__board__board_status', tool_input={})
        self.assertEqual(stamped['hookSpecificOutput']['updatedInput']['_route']['session_id'], 'chat')

    def test_board_prompt_warns_about_desktop_duplicates(self):
        profile = self.root / 'ClaudeProfiles/acc2'
        profile.mkdir(parents=True)
        (profile / 'claude_desktop_config.json').write_text(json.dumps({'mcpServers': {'board': {'command': 'x'}}}))
        reply = self.run_hook(hook_event_name='UserPromptSubmit', prompt='/board')
        self.assertIn('claude_desktop_config.json', reply['hookSpecificOutput']['additionalContext'])


class DesktopConfigTests(unittest.TestCase):
    def test_every_profile_config_is_found_and_only_board_is_removed(self):
        from board.desktop_config import duplicates, strip_board
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            default = home / 'AppData/Roaming/Claude/claude_desktop_config.json'
            profile = home / 'ClaudeProfiles/acc2/claude_desktop_config.json'
            clean = home / 'ClaudeProfiles/acc3/claude_desktop_config.json'
            for path, servers in ((default, {'board': {}, 'pdf': {}}), (profile, {'board': {}}), (clean, {'pdf': {}})):
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({'mcpServers': servers, 'preferences': {'x': 1}}))
            self.assertEqual(sorted(duplicates(home)), sorted([default, profile]))
            for path in duplicates(home):
                strip_board(path)
            self.assertEqual(duplicates(home), [])
            saved = json.loads(default.read_text())
            self.assertEqual(saved, {'mcpServers': {'pdf': {}}, 'preferences': {'x': 1}})
            self.assertTrue(list(default.parent.glob('claude_desktop_config.json.board-backup-*')))


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.me = self.auth(self.hub.call('register', {'name': 'me'}))
        self.old = time.time() - 30 * 86400

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def auth(registration):
        return {k: registration[k] for k in ('agent_id', 'session_token')}

    def call(self, method, **params):
        return self.hub.call(method, {**params, **self.me})

    def age(self, sql, *args):
        with self.hub.connect() as db:
            db.execute(sql, args)

    def count(self, sql, *args):
        with self.hub.connect() as db:
            return db.execute(sql, args).fetchone()[0]

    def prune(self):
        from board.retention import prune
        with self.hub.connect() as db:
            return prune(db, time.time())

    def test_old_finished_history_is_pruned_and_live_work_kept(self):
        peer = self.auth(self.hub.call('register', {'name': 'peer'}))
        self.call('noticeboard_post', body='old notice')
        acked = self.hub.call('inbox_send', {**peer, 'recipient_agent_id': self.me['agent_id'], 'body': 'old acked'})['id']
        self.call('inbox_ack', message_ids=[acked])
        self.hub.call('inbox_send', {**peer, 'recipient_agent_id': self.me['agent_id'], 'body': 'old unread'})
        done = self.call('job_post_and_claim', title='old done', description='d', acceptance='a', kind='noncode')['job']
        self.call('job_finish', job_id=done['id'], lease_token=done['lease_token'], summary='s',
                  checks=[{'command': 'none', 'result': 'not_applicable', 'evidence': 'noncode'}])
        live = self.call('job_post_and_claim', title='old but live', description='d', acceptance='a', kind='noncode')['job']
        for table, column in (('events', 'created'), ('posts', 'created'), ('messages', 'created'), ('jobs', 'updated')):
            self.age(f'UPDATE {table} SET {column}=?', self.old)
        self.age('UPDATE messages SET acknowledged=? WHERE acknowledged IS NOT NULL', self.old)
        self.call('noticeboard_post', body='recent notice')
        self.prune()
        self.assertEqual(self.count("SELECT COUNT(*) FROM posts WHERE body='old notice'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM posts WHERE body='recent notice'"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE body='old acked'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE body='old unread'"), 1)
        self.assertEqual(self.count('SELECT COUNT(*) FROM jobs WHERE id=?', done['id']), 0)
        self.assertEqual(self.count('SELECT COUNT(*) FROM jobs WHERE id=?', live['id']), 1)
        self.assertEqual(self.count('SELECT COUNT(*) FROM events WHERE created<?', time.time() - 86400), 0)

    def test_released_claims_pruned_active_claims_and_held_custody_kept(self):
        path = str(self.root / 'a.py')
        claim = self.call('claim', paths=[path], reason='r')['claim_ids']
        self.call('release', claim_ids=claim, held_reason='unfinished')
        self.call('claim', paths=[str(self.root / 'b.py')], reason='r')
        self.age('UPDATE claims SET started=?', self.old)
        self.age('UPDATE custody SET updated=?', self.old)
        self.prune()
        self.assertEqual(self.count('SELECT COUNT(*) FROM claims WHERE active=0'), 0)
        self.assertEqual(self.count('SELECT COUNT(*) FROM claims WHERE active=1'), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM custody WHERE state='HELD'"), 1)

    def test_idle_departed_agents_pruned_but_not_ones_with_work(self):
        idle = self.auth(self.hub.call('register', {'name': 'idle'}))
        busy = self.auth(self.hub.call('register', {'name': 'busy'}))
        self.hub.call('claim', {**busy, 'paths': [str(self.root / 'c.py')], 'reason': 'r'})
        self.call('retire', target_agent_id=idle['agent_id'], reason='gone')
        self.age("UPDATE agents SET seen=?, disconnected=? WHERE id IN (?,?)", self.old, self.old, idle['agent_id'], busy['agent_id'])
        self.prune()
        self.assertEqual(self.count('SELECT COUNT(*) FROM agents WHERE id=?', idle['agent_id']), 0)
        self.assertEqual(self.count('SELECT COUNT(*) FROM agents WHERE id=?', busy['agent_id']), 1)
        self.assertEqual(self.count('SELECT COUNT(*) FROM agents WHERE id=?', self.me['agent_id']), 1)

    def test_reset_wipes_everything_but_settings(self):
        self.call('noticeboard_post', body='x')
        self.call('job_post_and_claim', title='t', description='d', acceptance='a', kind='noncode')
        with self.assertRaises(BoardError):
            self.call('board_reset')  # Reason required.
        result = self.call('board_reset', reason='user typed /board reset')
        self.assertTrue(result['reset'])
        for table in ('agents', 'jobs', 'posts', 'claims', 'native_sessions'):
            self.assertEqual(self.count(f'SELECT COUNT(*) FROM {table}'), 0, table)
        self.assertEqual(self.count("SELECT COUNT(*) FROM settings WHERE key IN ('workspace','database_id')"), 2)


if __name__ == '__main__':
    unittest.main()
