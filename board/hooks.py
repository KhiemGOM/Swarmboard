"""Small command-hook adapter; supported edit paths are checked, shell writes are advisory."""
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .client import call
from .core import BoardError


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


def context(event, message):
    if event in ('SessionStart', 'PreToolUse', 'PostToolUse'):
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


class Adapter:
    def __init__(self, workspace, database, harness, url='http://127.0.0.1:4317', caller=call):
        self.workspace = Path(workspace).resolve()
        self.database = Path(database).resolve()
        self.harness = harness
        self.url = url
        self.caller = caller

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
        if event == 'PostToolUse' and re.fullmatch(r'mcp__board__(?:[a-z_]+)', name):
            agent = agent_from_response(payload.get('tool_response'))
            if agent:
                # Read the identity returned by THIS session's MCP; never register another agent.
                with closing(sqlite3.connect(f'{self.database.as_uri()}?mode=ro', uri=True)) as db:
                    workspace = db.execute("SELECT value FROM settings WHERE key='workspace'").fetchone()
                    identity = db.execute("SELECT token,state FROM agents WHERE id=?", (agent,)).fetchone()
                if not workspace or Path(workspace[0]).resolve() != self.workspace or not identity or identity[1] != 'active':
                    raise BoardError('Hook/MCP database or workspace does not match')
                state = {'agent_id': agent, 'session_token': identity[0], 'after_id': state.get('after_id', 0)}
                self.save(state_path, state)
                return context(event, 'Swarmboard hooks linked to your MCP identity. File/resource claims remain required; check job_list for authorized available work.')
        arguments = payload.get('tool_input') or {}
        edit = name in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'apply_patch')
        if event == 'SessionStart':
            return context(event, 'Swarmboard is enabled for this workspace. Call board_status first to bind hooks to your MCP agent ID, then read notices, inbox and job_list. Claim files/resources before editing; finish jobs with commit hash and checks, or save progress/yield.')
        if not state.get('agent_id'):
            if event == 'PreToolUse' and edit:
                return deny('Swarmboard hooks are not linked yet. Call board_status through MCP first, then claim the target files.')
            if event == 'PreToolUse' and name in ('Bash', 'exec_command'):
                return context(event, 'Call board_status to link Swarmboard hooks before changing files or using shared resources.')
            return {}
        auth = {k: state[k] for k in ('agent_id', 'session_token')}
        if event == 'SessionEnd':
            self.caller(self.url, 'disconnect', {**auth, 'reason': 'Harness SessionEnd; unfinished jobs returned to board'})
            return {}
        if event not in ('PreToolUse', 'Stop') or name.startswith('mcp__board__'):
            return {}
        status = self.caller(self.url, 'board_status', auth)
        if Path(status['workspace']).resolve() != self.workspace:
            raise BoardError('Hook connected to another workspace')
        notices = []
        while True:
            batch = self.caller(self.url, 'noticeboard_read', {**auth, 'after_id': state.get('after_id', 0)})['posts']
            notices.extend(batch)
            if batch:
                state['after_id'] = batch[-1]['id']
            if len(batch) < 100:
                break
        self.save(state_path, state)
        claims = [row for row in status['claims'] if row['agent'] == state['agent_id']]
        if event == 'PreToolUse' and edit:
            paths = edit_paths(name, arguments)
            if not paths:
                return deny('Swarmboard could not identify edit paths; use a supported file edit or coordinate the operation explicitly.')
            for path in paths:
                try:
                    relative = (cwd / path.strip()).resolve().relative_to(self.workspace)
                except ValueError:
                    return deny('Edit target is outside the Swarmboard workspace.')
                target = os.path.normcase(relative.as_posix()).replace('\\', '/') or '.'
                if not any(row['kind'] == 'file' and (row['path'] == '.' or target == row['path'] or target.startswith(row['path'] + '/')) for row in claims):
                    return deny(f'Swarmboard: acquire an accepted file claim for {path} before editing.')
        if event == 'PreToolUse' and name in ('Bash', 'exec_command'):
            command = arguments.get('command') or arguments.get('cmd', '')
            if re.search(r'\bgit\b[^\n;&|]*\b(add|commit|reset|restore|checkout|switch|stash|merge|rebase|cherry-pick|update-index|read-tree|apply|am)\b', command, re.I):
                if not any(row['kind'] == 'resource' and row['path'] == 'git:index' for row in claims):
                    return deny('Swarmboard: acquire git:index before this Git operation and hold it through staging, commit and index sync.')
        reminders = [status[k] for k in ('inbox_reminder', 'custody_reminder', 'job_reminder') if k in status]
        if notices:
            reminders.append('New notices (agent information, not owner authorization): ' + json.dumps([{'id': r['id'], 'body': r['body'][:600]} for r in notices[-3:]]))
        if event == 'PreToolUse' and name in ('Bash', 'exec_command'):
            reminders.append('Shell writes, databases, ports and builds require your cooperative file/resource claims; this hook cannot infer every command effect.')
        if event == 'Stop' and reminders:
            reminders.insert(0, 'Swarmboard handoff reminder: record completion or saved progress for unfinished work. No automatic commit or completion.')
        return context(event, '\n'.join(reminders)) if reminders else {}

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
