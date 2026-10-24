"""Small command-hook adapter; supported edit paths are checked, shell writes are advisory."""
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
from contextlib import closing
from pathlib import Path

from .client import call
from .core import BoardError
from .rules import JOB_REQUIREMENT


def agent_from_response(value):
    if isinstance(value, str):
        try:
            return agent_from_response(json.loads(value))
        except (ValueError, RecursionError):
            return None
    if isinstance(value, dict):
        agent = value.get('your_agent_id')
        if isinstance(agent, str) and re.fullmatch('[0-9a-f]{16}', agent):
            return agent
        for key in ('content', 'text', 'result', 'structuredContent'):
            if key in value:
                found = agent_from_response(value[key])
                if found:
                    return found
    if isinstance(value, list):
        for item in value:
            found = agent_from_response(item)
            if found:
                return found
    return None


OPT_IN = ('Swarmboard is opt-in per chat and is not active here. Work as in a normal repository; '
          'board tools become available after the user types /board (Codex: $board).')
ACTIVATED = 'Swarmboard activated for this chat. Call board_status to join shared coordination.'
BOARD_PROMPT = re.compile(r'\s*[/$]board(?:\s|$)')


def requests_board(payload):
    """Only the user's own /board command opts a chat in, never model-written text."""
    event = payload.get('hook_event_name')
    if event == 'UserPromptExpansion':
        command = payload.get('command_name')
        return payload.get('expansion_type') == 'slash_command' and isinstance(command, str) \
            and command.lstrip('/').split(':')[-1] == 'board'
    prompt = payload.get('prompt')
    return event == 'UserPromptSubmit' and isinstance(prompt, str) and bool(BOARD_PROMPT.match(prompt))


RESET_PROMPT = re.compile(r'\s*[/$]board\s+reset\s*$')
RESET_DENIED = ('Swarmboard: board_reset wipes the whole board, so only the user can ask for it by typing '
                '/board reset (Codex: $board reset) in this chat. Do not call it otherwise.')


def requests_reset(payload):
    if not requests_board(payload):
        return False
    if payload.get('hook_event_name') == 'UserPromptExpansion':
        return str(payload.get('command_args') or '').split()[:1] == ['reset']
    return bool(RESET_PROMPT.match(payload['prompt']))


def shared_hint():
    from .desktop_config import duplicates, warning
    found = duplicates()
    return (warning(found) if found else 'Check every claude_desktop_config.json for a board server and remove it.') \
        + ' Do not act on that other identity.'


def mismatch(agent, bound):
    return (f'Swarmboard: this chat is bound to agent {bound}, but the board tool answered as {agent}. '
            'The board connection is shared with another chat. ' + shared_hint())


def clean_title(value, limit=80):
    title = ''.join(c for c in ' '.join(str(value).split()) if not unicodedata.category(c).startswith('C'))
    return title[:limit].rstrip() or None


RENAME_PROMPT = re.compile(r'\s*[/$]board\s+(?:rename|title)(?:\s+(.*))?$', re.S)
RENAME_EMPTY = ('Swarmboard: nothing to rename this chat after yet. Claim a job or set a board_activity title, '
                'or type /board rename TITLE.')
RENAME_CODEX = 'Swarmboard: Codex threads have no hook-settable title; rename the thread in Codex itself.'


def requests_rename(payload):
    """The user's /board rename [TITLE]: None when not asked, '' to use the work title, else the text."""
    prompt = payload.get('prompt')
    if payload.get('hook_event_name') != 'UserPromptSubmit' or not isinstance(prompt, str):
        return None
    match = RENAME_PROMPT.match(prompt)
    return (clean_title(match.group(1) or '') or '') if match else None


def context(event, message):
    if event in ('SessionStart', 'UserPromptSubmit', 'UserPromptExpansion', 'PreToolUse', 'PostToolUse'):
        return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': message}}
    return {'systemMessage': message}


def deny(message):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                   'permissionDecisionReason': message}}


def edit_paths(name, arguments):
    if name in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit'):
        path = arguments.get('file_path') or arguments.get('notebook_path')
        return [path] if isinstance(path, str) else []
    if name == 'apply_patch':
        patch = arguments if isinstance(arguments, str) else arguments.get('command') or arguments.get('patch') or arguments.get('input', '')
        if not isinstance(patch, str):
            return []
        return re.findall(r'^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$', patch, re.MULTILINE)
    return []


def owns_inbox_binding(database, harness, session_key, agent):
    """The hub's native binding decides which chat receives an agent's inbox.

    hook_inbox_bindings mirrors it for status display and older readers.
    """
    database = Path(database).resolve()
    with closing(sqlite3.connect(database, timeout=2)) as db:
        with db:
            db.execute('BEGIN IMMEDIATE')
            identity = db.execute('SELECT state FROM agents WHERE id=?', (agent,)).fetchone()
            from .sessions import initialize
            initialize(db)
            bound = db.execute('SELECT agent FROM native_sessions WHERE harness=? AND session_key=?',
                               (harness, session_key)).fetchone()
            if not identity or identity[0] != 'active' or not bound or bound[0] != agent:
                return False
            db.execute('''CREATE TABLE IF NOT EXISTS hook_inbox_bindings (
                harness TEXT NOT NULL, agent TEXT NOT NULL, session_key TEXT NOT NULL,
                PRIMARY KEY(harness,agent), UNIQUE(harness,session_key))''')
            db.execute('DELETE FROM hook_inbox_bindings WHERE harness=? AND (agent=? OR session_key=?)',
                       (harness, agent, session_key))
            db.execute('INSERT INTO hook_inbox_bindings VALUES (?,?,?)', (harness, agent, session_key))
            return True


class Adapter:
    def __init__(self, workspace, database, harness, url='http://127.0.0.1:4317', caller=call):
        self.workspace = Path(workspace).resolve()
        self.database = Path(database).resolve()
        self.harness = harness
        self.url = url
        self.caller = caller

    def bound(self, key):
        """This chat's identity is the hub's native binding; nothing else is trusted or cached."""
        if not self.database.is_file():
            return None
        with closing(sqlite3.connect(f'{self.database.as_uri()}?mode=ro', uri=True, timeout=2)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='native_sessions'").fetchone():
                return None
            row = db.execute('''SELECT a.id,a.token,a.state,a.disconnected FROM native_sessions n
                                JOIN agents a ON a.id=n.agent WHERE n.harness=? AND n.session_key=?''',
                             (self.harness, key)).fetchone()
        return dict(zip(('agent_id', 'session_token', 'state', 'disconnected'), row)) if row else None

    def adopt(self, key, agent):
        """A response from an older, unrouted bridge may name this chat's agent; bind it once."""
        if not self.database.is_file():
            return False
        from .sessions import bind
        with closing(sqlite3.connect(self.database, timeout=2)) as db:
            with db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT state FROM agents WHERE id=?', (agent,)).fetchone()
                if not row or row[0] != 'active':
                    return False
                try:
                    bind(db, self.harness, key, agent)
                except BoardError:
                    return False
        return True

    def declare(self, key, agent):
        if not self.database.is_file():
            return False
        from .sessions import identify
        with closing(sqlite3.connect(self.database, timeout=2)) as db:
            with db:
                db.execute('BEGIN IMMEDIATE')
                if not db.execute('SELECT 1 FROM agents WHERE id=?', (agent,)).fetchone():
                    return False
                identify(db, self.harness, key, agent)
        return True

    def handle(self, payload):
        event = payload.get('hook_event_name')
        cwd = Path(payload.get('cwd') or '.').resolve()
        try:
            cwd.relative_to(self.workspace)
        except ValueError:
            return {}  # A global hook only coordinates its configured workspace.
        session_id = payload.get('session_id')
        if not isinstance(session_id, str) or not session_id:
            raise BoardError('Hook payload is missing session_id')
        key = hashlib.sha256((self.harness + ':' + session_id).encode()).hexdigest()
        state_path = self.database.parent / 'hook-sessions' / (key + '.json')
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        name = payload.get('tool_name', '')
        activated = False
        if requests_board(payload):
            activated = not state.get('activated')
            state['activated'] = True
            # Only the prompt that asked for a wipe authorizes it; any later prompt withdraws it.
            state['reset_requested'] = requests_reset(payload)
            self.save(state_path, state)
        elif event == 'UserPromptSubmit' and state.pop('reset_requested', None):
            self.save(state_path, state)
        if name.startswith('mcp__board__') and event == 'PreToolUse':
            # Chats linked before opt-in existed already chose to join.
            if not state.get('activated') and not state.get('agent_id'):
                return deny(OPT_IN)
            if name == 'mcp__board__board_reset':
                if not state.pop('reset_requested', None):
                    return deny(RESET_DENIED)
                self.save(state_path, state)
            # Every board call carries THIS chat's route; the bridge may be shared by other chats.
            route = {'harness': self.harness, 'session_id': session_id, 'cwd': str(cwd)}
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                           'updatedInput': {**(payload.get('tool_input') or {}), '_route': route}}}
        if event == 'UserPromptExpansion':
            return context(event, ACTIVATED) if activated else {}
        notes = []
        if event == 'PostToolUse' and name == 'mcp__board__board_workspace':
            response = payload.get('tool_response') or {}
            if isinstance(response, dict) and not response.get('isError'):
                return context(event, 'Board workspace connection checked. Call board_status on the new board. '
                               'Installed hooks/watchers remain scoped to the project of this chat folder.')
        identity = self.bound(key)
        if event == 'PostToolUse' and name.startswith('mcp__board__') and name != 'mcp__board__board_workspace':
            # board_workspace answers from another project's board; this hook coordinates its own.
            response = payload.get('tool_response')
            if 'WORKSPACE_REQUIRED' in json.dumps(response):
                return context(event, 'Swarmboard has no project for this chat folder. Type /board in a chat opened in the '
                               'project, or call board_workspace with the absolute project directory and a reason, then board_status.')
            agent = agent_from_response(response)
            failed = isinstance(response, dict) and response.get('isError')
            if agent and name == 'mcp__board__board_identity' and not failed and self.declare(key, agent):
                identity = self.bound(key)  # An explicit declaration rebinds this chat.
            elif agent and identity and identity['agent_id'] != agent:
                return context(event, mismatch(agent, identity['agent_id']))
            elif agent and not identity:
                if not self.adopt(key, agent):
                    return context(event, f'Swarmboard refused a shared inbox binding: agent {agent} belongs to another chat. '
                                   'This chat will not watch or consume that inbox; the board connection is shared. ' + shared_hint())
                identity = self.bound(key)
        rename = requests_rename(payload)
        if not identity or identity['state'] != 'active' or identity['disconnected'] is not None:
            output = context(event, ACTIVATED) if activated else {}
            if rename is not None:
                # Unlinked: there is no work title yet, but explicit text can still name the chat.
                title, note = self.rename(state_path, state, None, rename)
                output = self.titled(output, event, title, note)
            return output
        if event == 'SessionEnd':
            params = {'agent_id': identity['agent_id'], 'session_token': identity['session_token'],
                      'reason': 'Harness SessionEnd: ' + str(payload.get('reason') or 'other')}
            if self.caller is call:
                self.caller(self.url, 'session_end', params, timeout=1)
            else:
                self.caller(self.url, 'session_end', params)
            return {}
        if state.get('agent_id') != identity['agent_id']:
            # The inbox watcher reads this; it mirrors the binding and is never trusted on its own.
            state['agent_id'] = identity['agent_id']
            self.save(state_path, state)
        if not owns_inbox_binding(self.database, self.harness, key, identity['agent_id']):
            return {}
        if state.get('linked') != identity['agent_id']:
            state['linked'] = identity['agent_id']
            self.save(state_path, state)
            notes.append(JOB_REQUIREMENT + ' Swarmboard hooks linked to your MCP identity. File/resource claims remain required.')
        auth = {'agent_id': identity['agent_id'], 'session_token': identity['session_token']}
        if event not in ('PreToolUse', 'PostToolUse', 'UserPromptSubmit', 'Stop'):
            return {}
        arguments = payload.get('tool_input') or {}
        edit = name in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'apply_patch')
        if self.harness == 'codex':
            self.ensure_desktop_wake(auth, session_id, event)
        status = self.hook_status(auth, payload)
        if Path(status['workspace']).resolve() != self.workspace:
            raise BoardError('Hook connected to another workspace')
        if event == 'PostToolUse' and (edit or name in ('Bash', 'exec_command')):
            self.caller(self.url, 'board_file', {**auth, 'action': 'reconcile'})
        if event in ('PostToolUse', 'UserPromptSubmit'):
            inbox = self.inbox_context(status, auth)
            if inbox:
                notes.append(inbox)
            output = context(event, '\n'.join(notes)) if notes else {}
            if rename is not None:
                return self.titled(output, event, *self.rename(state_path, state, identity['agent_id'], rename))
            title = self.sync_title(state_path, state, payload) if event == 'UserPromptSubmit' else None
            return self.titled(output, event, title)
        notices = []
        while True:
            batch = self.caller(self.url, 'noticeboard_read', {**auth, 'after_id': state.get('after_id', 0), 'limit': 1})['posts']
            notices.extend(batch)
            if batch:
                state['after_id'] = batch[-1]['id']
            if not batch:
                break
        self.save(state_path, state)
        claims = [row for row in status['claims'] if row['agent'] == identity['agent_id']]
        inbox = self.inbox_context(status, auth)
        owned_jobs = status.get('owned_jobs_count', len(status.get('owned_jobs', [])) or int(bool(status.get('job_reminder'))))
        job_warning = 'No owned job. ' + JOB_REQUIREMENT if event == 'PreToolUse' and edit and not owned_jobs else ''
        def blocked(message):
            return deny(message + ('\n' + job_warning if job_warning else '') + ('\n' + inbox if inbox else ''))
        if event == 'PreToolUse' and edit:
            paths = edit_paths(name, arguments)
            if not paths:
                return blocked('Swarmboard could not identify edit paths; use a supported file edit or coordinate the operation explicitly.')
            for path in paths:
                try:
                    relative = (cwd / path.strip()).resolve().relative_to(self.workspace)
                except ValueError:
                    return blocked('Edit target is outside the Swarmboard workspace.')
                target = os.path.normcase(relative.as_posix()).replace('\\', '/') or '.'
                with closing(sqlite3.connect(f'{self.database.as_uri()}?mode=ro', uri=True)) as db:
                    queues = db.execute("SELECT id,path FROM patch_batches WHERE state IN ('OPEN','READY','APPLYING')").fetchall()
                if any(target == row[1] or target.startswith(row[1] + '/') or row[1].startswith(target + '/') for row in queues):
                    return blocked('Swarmboard: this file has a patch queue lock. Submit a patch through patch_queue; direct edits are forbidden until finalization.')
                from .claims import covers
                if not any(row['kind'] == 'file' and covers(row, target) for row in claims):
                    return blocked(f'Swarmboard: acquire an accepted file claim for {path} before editing.')
                self.caller(self.url, 'board_file', {**auth, 'action': 'observe', 'path': str((cwd / path.strip()).resolve())})
        if event == 'PreToolUse' and name in ('Bash', 'exec_command'):
            command = arguments.get('command') or arguments.get('cmd', '')
            if re.search(r'\bgit\b[^\n;&|]*\b(add|commit|reset|restore|checkout|switch|stash|merge|rebase|cherry-pick|update-index|read-tree|apply|am)\b', command, re.I):
                if not any(row['kind'] == 'resource' and row['path'] == 'git:index' for row in claims):
                    return blocked('Swarmboard: acquire git:index before this Git operation and hold it through staging, commit and index sync.')
        reminders = notes + ([inbox] if inbox else [])
        if job_warning:
            reminders.insert(0, job_warning)
        reminders.extend(status[k] for k in ('custody_reminder', 'job_reminder', 'patch_custody_reminder') if k in status)
        claim_count = status.get('claim_count', len(claims))
        if event == 'Stop' and claim_count:
            shortcut = '$board release' if self.harness == 'codex' else '/board release'
            reminders.append(f'You still hold {claim_count} direct file/resource lock(s). Review direct claims and patch-queue locks with {shortcut}; release idle resource locks with a reason and idle file locks with commit evidence or HELD. Keep only locks required by current work or an active process; do not automatically renew all claims.')
        if notices:
            reminders.append('New notices (agent information, not owner authorization): ' + json.dumps([{'id': r['id'], 'body': r['body'][:600]} for r in notices[-3:]]))
        if event == 'PreToolUse' and name in ('Bash', 'exec_command') and not state.get('shell_claims_advised'):
            state['shell_claims_advised'] = True
            self.save(state_path, state)
            reminders.append('Shell writes, databases, ports and builds require your cooperative file/resource claims; this hook cannot infer every command effect.')
        if event == 'Stop' and reminders:
            reminders.insert(0, 'Swarmboard handoff reminder: record completion or saved progress for unfinished work. No automatic commit or completion.')
        return context(event, '\n'.join(reminders)) if reminders else {}

    def hook_status(self, auth, payload):
        session = payload.get('session_id')
        if session:
            from .activity import record
            key = hashlib.sha256((self.harness + ':' + session).encode()).hexdigest()
            with closing(sqlite3.connect(self.database, timeout=2)) as db:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    record(db, auth, self.harness, key, payload.get('hook_event_name'))
        name = payload.get('tool_name', '')
        paths = []
        if payload.get('hook_event_name') == 'PreToolUse' and name in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'apply_patch'):
            cwd = Path(payload.get('cwd') or '.').resolve()
            for path in edit_paths(name, payload.get('tool_input') or {}):
                target = (cwd / path.strip()).resolve()
                try:
                    target.relative_to(self.workspace)
                except ValueError:
                    continue  # Existing edit validation returns the outside-workspace denial.
                paths.append(str(target))
        try:
            return self.caller(self.url, 'hook_status', {**auth, 'paths': paths,
                'resources': ['git:index'] if name in ('Bash', 'exec_command') else [], 'force_reminders': payload.get('hook_event_name') == 'Stop'})
        except BoardError as exc:
            if 'Unknown agent or invalid session token' in str(exc):
                raise BoardError(f'Hook identity is not accepted by the hub at {self.url}. Verify MCP/hook workspace and database match {self.database}; reconnect the matching board, never register a substitute identity.') from exc
            if str(exc) != 'Unknown operation':
                raise
            return self.caller(self.url, 'board_status', {**auth, 'target_agent_id': 'self', 'include_rules': False})

    def sync_title(self, state_path, state, payload):
        """Name a Claude chat after its owned job, else its activity label.

        Only a changed source is sent, so a manual rename survives until the work changes.
        """
        if self.harness != 'claude':
            return None  # Codex has no hook-settable thread title.
        title = self.work_title(state['agent_id'])
        if not title or title == state.get('synced_title'):
            return None
        state['synced_title'] = title
        self.save(state_path, state)
        return None if title == payload.get('session_title') else title

    def work_title(self, agent):
        with closing(sqlite3.connect(f'{self.database.as_uri()}?mode=ro', uri=True)) as db:
            row = db.execute("SELECT title FROM jobs WHERE owner=? AND state='CLAIMED' ORDER BY updated DESC, id DESC LIMIT 1",
                             (agent,)).fetchone()
            if not row and db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_activity'").fetchone():
                row = db.execute('SELECT title FROM agent_activity WHERE agent=?', (agent,)).fetchone()
        return clean_title(row[0]) if row and row[0] else None

    def rename(self, state_path, state, agent, text):
        """/board rename: name the chat now, even over a manual rename. Returns (title, note).

        The current work title is recorded as synced, so explicit text lasts until the work changes.
        """
        if self.harness != 'claude':
            return None, RENAME_CODEX
        source = self.work_title(agent) if agent else None
        if source:
            state['synced_title'] = source
            self.save(state_path, state)
        title = text or source
        return (title, None) if title else (None, RENAME_EMPTY)

    @staticmethod
    def titled(output, event, title, note=None):
        if note:
            specific = output.setdefault('hookSpecificOutput', {'hookEventName': event})
            specific['additionalContext'] = '\n'.join(filter(None, (specific.get('additionalContext'), note)))
        if title:
            output.setdefault('hookSpecificOutput', {'hookEventName': event})['sessionTitle'] = title
        return output

    def ensure_desktop_wake(self, auth, native_chat, event=None):
        """Restore the default from trusted native hook context, never model text."""
        self.record_wake_activity(auth, native_chat, event)
        status = self.caller(self.url, 'board_desktop_wake', {**auth, 'action': 'status'})
        if status['enabled']:
            if status['thread_id'] != native_chat:
                raise BoardError('Codex wake binding belongs to a different native chat')
        else:
            self.caller(self.url, 'board_desktop_wake', {**auth, 'action': 'enable',
                'thread_id': native_chat, 'workspace': str(self.workspace), 'include_existing': True,
                'reason': 'Automatic inbox wake from verified exclusive Codex native hook identity',
                'owner_authorization': 'OWNER_REQUESTED_DESKTOP_WAKE'})
        self.record_wake_activity(auth, native_chat, event)

    def record_wake_activity(self, auth, native_chat, event):
        if event not in ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop'):
            return
        # Only a verified native hook updates activity, never a model-supplied tool argument.
        from .desktop_wake import initialize
        with closing(sqlite3.connect(self.database, timeout=2)) as db:
            with db:
                db.execute('BEGIN IMMEDIATE')
                initialize(db)
                db.execute('UPDATE desktop_wake_bindings SET activity=?,activity_updated=?,activity_event=? '
                           'WHERE agent=? AND thread_id=? AND EXISTS '
                           "(SELECT 1 FROM agents WHERE id=? AND token=? AND state='active')",
                           ('idle' if event == 'Stop' else 'busy', time.time(), event,
                            auth['agent_id'], native_chat, auth['agent_id'], auth['session_token']))

    def inbox_context(self, status, auth):
        if not status.get('unread_inbox'):
            return ''
        messages = status.get('inbox_preview')
        if messages is None:
            messages = self.caller(self.url, 'inbox_read', {**auth, 'limit': 1})['messages']
        if not messages:
            return ''
        preview = [{'id': row['id'], 'sender': row['sender'], 'body': row['body'][:1000]} for row in messages[:3]]
        return (status.get('inbox_reminder', 'You have unread inbox messages.') +
                '\n' + json.dumps(preview))

    @staticmethod
    def save(path, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            stream.write(json.dumps(state))
            temp = Path(stream.name)
        try:
            if os.name != 'nt':
                temp.chmod(0o600)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
