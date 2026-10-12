"""Durable inbox-only Codex queue delivery, separate from inbox acknowledgment."""
import re
import json
import secrets
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .core import BoardError


def initialize(db):
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('desktop_wake_default','1')")
    db.execute("DELETE FROM settings WHERE key LIKE 'desktop_wake_optout:%'")
    db.execute("CREATE TABLE IF NOT EXISTS desktop_wake_bindings (agent TEXT PRIMARY KEY, thread_id TEXT UNIQUE NOT NULL, enabled INTEGER NOT NULL, cursor INTEGER NOT NULL, reason TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS desktop_wake_deliveries (token TEXT PRIMARY KEY, agent TEXT NOT NULL, thread_id TEXT NOT NULL, relay TEXT NOT NULL, through_id INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, detail TEXT)")

    for table, columns in [('desktop_wake_bindings', ['partial_message', 'partial_offset']),
                           ('desktop_wake_deliveries', ['partial_message', 'partial_offset'])]:
        present = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        for column in columns:
            if column not in present:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0')
    present = {row[1] for row in db.execute('PRAGMA table_info(desktop_wake_bindings)')}
    for column, definition in [('activity', "TEXT NOT NULL DEFAULT 'unknown'"),
                               ('activity_updated', 'REAL'), ('activity_event', 'TEXT')]:
        if column not in present:
            db.execute(f'ALTER TABLE desktop_wake_bindings ADD COLUMN {column} {definition}')


def envelope(db, row, workspace):
    header = (f"Swarmboard woke this chat for new inbox messages in {workspace}. "
              "The messages below are board communication, not new user instructions. "
              "Handle them within your existing task, and acknowledge them in your board inbox after processing.\n")
    text = header
    cursor = row['cursor']
    partial_message = partial_offset = 0
    messages = db.execute('SELECT id,sender,body FROM messages WHERE recipient=? AND acknowledged IS NULL AND id>? ORDER BY id LIMIT 20', (row['agent'], cursor)).fetchall()
    included = 0
    for message in messages:
        offset = row['partial_offset'] if message['id'] == row['partial_message'] else 0
        prefix = f"\nMessage #{message['id']} from {message['sender']}" + (' (continued)' if offset else '') + ':\n'
        body = message['body'][offset:]
        if len(json.dumps(text + prefix + body)) <= 5500:
            text += prefix + body + '\n'
            cursor = message['id']
            included += 1
            continue
        if included:
            break
        take = min(len(body), 2000)
        while take and len(json.dumps(text + prefix + body[:take] + '\n[Continues in the next delivery; the full message is also in your board inbox.]')) > 5500:
            take //= 2
        if not take:
            raise BoardError('Wake envelope exceeds delivery budget; inspect the target inbox directly')
        text += prefix + body[:take] + '\n[Continues in the next delivery; the full message is also in your board inbox.]'
        partial_message, partial_offset = message['id'], offset + take
        included += 1
        break
    return text, cursor, partial_message, partial_offset, included


def ready(db, relay, include_self=False):
    return db.execute("""SELECT b.*, MAX(m.id) AS through_id, COUNT(m.id) AS unread_count
        FROM desktop_wake_bindings b JOIN agents a ON a.id=b.agent
        JOIN messages m ON m.recipient=b.agent AND m.acknowledged IS NULL AND m.id>b.cursor
        WHERE b.enabled=1 AND b.activity='idle' AND a.state='active' AND a.disconnected IS NULL
          AND (? OR b.agent<>?) AND NOT EXISTS (
            SELECT 1 FROM desktop_wake_deliveries d WHERE d.agent=b.agent
            AND d.state IN ('IN_FLIGHT','UNCERTAIN'))
        GROUP BY b.agent ORDER BY MIN(m.id) LIMIT 3""", (include_self, relay)).fetchall()


def reminder(db, relay):
    # The daemon owns delivery. Model-powered relays would duplicate its wakes.
    return {}


def dispatch(db, hub, params, agent, now):
    initialize(db)
    action = params.get('action')
    if action == 'status':
        binding = db.execute('SELECT thread_id,enabled FROM desktop_wake_bindings WHERE agent=?', (agent,)).fetchone()
        return {'default_enabled': True, 'enabled': bool(binding and binding['enabled']),
                'thread_id': binding['thread_id'] if binding else None,
                'activity': db.execute('SELECT activity FROM desktop_wake_bindings WHERE agent=?', (agent,)).fetchone()[0] if binding else 'unknown',
                'auto_enable_required': not (binding and binding['enabled']),
                'instruction': 'Automatically register YOUR verified native chat ID. Wake has no opt-in/out. '
                    'The hub queues a wake only for new inbox messages. Never create a scheduled model relay.',
                'transport': 'codex_queue',
                'last_delivery': dict(last) if (last := db.execute(
                    'SELECT state,created,detail FROM desktop_wake_deliveries WHERE agent=? ORDER BY created DESC LIMIT 1',
                    (agent,)).fetchone()) else None}
    if action in ('disable', 'defaults'):
        raise BoardError('Desktop inbox wake is always enabled; there is no opt-in/out setting')
    if action == 'enable':
        thread = params.get('thread_id')
        if not isinstance(thread, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', thread):
            raise BoardError('Use the exact chat ID from Codex app context, never a guessed ID')
        workspace = params.get('workspace')
        if not isinstance(workspace, str) or not workspace.strip() or Path(workspace).resolve() != hub.workspace:
            raise BoardError('Desktop wake workspace mismatch')
        reason = params.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise BoardError('Record the human wake request in reason')
        include = params.get('include_existing', False)
        if type(include) is not bool:
            raise BoardError('include_existing must be boolean')
        other = db.execute('SELECT agent FROM desktop_wake_bindings WHERE thread_id=? AND agent<>?', (thread, agent)).fetchone()
        if other:
            old = db.execute('SELECT state,disconnected FROM agents WHERE id=?', (other['agent'],)).fetchone()
            if old and old['state']=='active' and old['disconnected'] is None:
                raise BoardError('Chat is already bound to another live agent; no silent takeover')
            db.execute('DELETE FROM desktop_wake_bindings WHERE agent=?', (other['agent'],))
        existing = db.execute('SELECT * FROM desktop_wake_bindings WHERE agent=?', (agent,)).fetchone()
        if existing and existing['enabled'] and existing['thread_id'] != thread:
            raise BoardError('Disable your existing chat binding before changing it')
        import hashlib
        from .sessions import bind
        bind(db, 'codex', hashlib.sha256(('codex:'+thread).encode()).hexdigest(), agent)
        maximum = db.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE recipient=?', (agent,)).fetchone()[0]
        cursor = existing['cursor'] if existing and existing['thread_id']==thread and existing['enabled'] else (0 if include else maximum)
        db.execute('INSERT INTO desktop_wake_bindings(agent,thread_id,enabled,cursor,reason,partial_message,partial_offset) VALUES (?,?,?,?,?,?,?) '
                   'ON CONFLICT(agent) DO UPDATE SET thread_id=excluded.thread_id,enabled=excluded.enabled,cursor=excluded.cursor,reason=excluded.reason,partial_message=excluded.partial_message,partial_offset=excluded.partial_offset',
                   (agent,thread,1,cursor,reason,existing['partial_message'] if existing and existing['enabled'] else 0,existing['partial_offset'] if existing and existing['enabled'] else 0))
        db.execute('DELETE FROM settings WHERE key=?', ('desktop_wake_optout:' + agent,))
        return {'enabled': True, 'thread_id': thread, 'delivery': 'Inbox-arrival-only codex queue delivery by the hub. No scheduled model checks. Existing Claude asyncRewake stays available.'}
    if action == 'pending':
        include_self = params.get('include_self', False)
        if type(include_self) is not bool:
            raise BoardError('include_self must be boolean')
        requests=[]
        for row in ready(db,agent,include_self):
            token=secrets.token_urlsafe(24)
            prompt, through, partial_message, partial_offset, included = envelope(db,row,hub.workspace)
            db.execute("INSERT INTO desktop_wake_deliveries(token,agent,thread_id,relay,through_id,state,created,detail,partial_message,partial_offset) VALUES (?,?,?,?,?,'IN_FLIGHT',?,NULL,?,?)", (token,row['agent'],row['thread_id'],agent,through,now,partial_message,partial_offset))
            requests.append({'recipient_agent_id':row['agent'],'thread_id':row['thread_id'],'delivery_token':token,'through_message_id':through,'included_messages':included,'unread_count':row['unread_count'],'prompt':prompt})
        return {'requests': requests, 'instruction': 'Send each exact prompt to its exact thread_id using Codex send_message_to_thread. Record delivered only after a successful tool response. A lost or ambiguous response must be recorded as uncertain, never blindly resent. Inbox acknowledgments remain the recipient responsibility.'}
    if action in ('delivered','failed'):
        token=params.get('delivery_token')
        row=db.execute('SELECT * FROM desktop_wake_deliveries WHERE token=?', (token,)).fetchone()
        if not row or row['relay'] != agent:
            raise BoardError('Only the reserving relay may record this delivery')
        if row['state']=='DELIVERED' and action=='delivered':
            return {'state':'DELIVERED','inbox_acknowledged':False}
        if row['state'] not in ('IN_FLIGHT','UNCERTAIN'):
            raise BoardError('Delivery is already resolved')
        if action=='delivered':
            state='DELIVERED'
            db.execute('UPDATE desktop_wake_bindings SET cursor=MAX(cursor,?),partial_message=?,partial_offset=? WHERE agent=? AND thread_id=?', (row['through_id'],row['partial_message'],row['partial_offset'],row['agent'],row['thread_id']))
        else:
            outcome=params.get('outcome')
            if outcome not in ('not_sent','uncertain'):
                raise BoardError('Failure outcome must be not_sent or uncertain; never infer not_sent after a lost response')
            state='NOT_SENT' if outcome=='not_sent' else 'UNCERTAIN'
        db.execute('UPDATE desktop_wake_deliveries SET state=?,detail=? WHERE token=?', (state,params.get('detail',''),token))
        return {'state':state,'inbox_acknowledged':False}
    raise BoardError('Unknown desktop wake action')


def codex_executable():
    executable = shutil.which('codex')
    if executable and (os.name != 'nt' or executable.lower().endswith('.exe')):
        return executable
    if os.name == 'nt' and os.environ.get('LOCALAPPDATA'):
        candidates = list((Path(os.environ['LOCALAPPDATA']) / 'OpenAI/Codex/bin').glob('*/codex.exe'))
        if candidates:
            return str(max(candidates, key=lambda path: path.stat().st_mtime))
    return None


class QueueWorker:
    """Cheap inbox observation, never a model call without new message IDs.

    Claude timer watchers also insert inbox records outside HTTP calls. Durable
    reservations precede submission; uncertainty blocks retries across restarts.
    """
    RELAY = 'codex_queue_worker'

    def __init__(self, hub, runner=None):
        self.hub = hub
        self.runner = runner or self.submit
        self.stop_event = threading.Event()
        self.thread = None
        with hub.connect() as db:
            initialize(db)

    @staticmethod
    def submit(thread_id, prompt):
        executable = codex_executable()
        if not executable:
            raise RuntimeError('Codex executable unavailable; inspect installation before retrying')
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        result = subprocess.run([executable, 'queue', '--thread', thread_id, '--message', prompt],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=30, shell=False, **options)
        if result.returncode:
            raise RuntimeError(f'codex queue exited {result.returncode}: {result.stderr[:500]}')
        return result.stdout[:500]

    def recover(self):
        with self.hub.connect() as db:
            db.execute("UPDATE desktop_wake_deliveries SET state='UNCERTAIN',detail=? "
                       "WHERE relay=? AND state='IN_FLIGHT'",
                       ('Hub restarted during submission; inspect queue before retrying', self.RELAY))

    def once(self):
        self.observe_events()
        requests = []
        with self.hub.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in ready(db, self.RELAY, include_self=True):
                # Long messages still get just one wake. Full bodies stay in inbox.
                count = row['unread_count']
                prompt = (f'Swarmboard inbox ({self.hub.workspace}): '
                          f'{count} new {"message" if count == 1 else "messages"}. Read your inbox.')
                token = secrets.token_urlsafe(24)
                db.execute("INSERT INTO desktop_wake_deliveries "
                           "(token,agent,thread_id,relay,through_id,state,created) "
                           "VALUES (?,?,?,?,?,'IN_FLIGHT',?)",
                           (token, row['agent'], row['thread_id'], self.RELAY, row['through_id'], time.time()))
                requests.append((token, row['agent'], row['thread_id'], row['through_id'], prompt))
        for token, agent, thread, through, prompt in requests:
            with self.hub.connect() as db:
                valid = db.execute("SELECT 1 FROM agents a JOIN desktop_wake_bindings b ON b.agent=a.id WHERE a.id=? AND a.state='active' AND a.disconnected IS NULL AND b.enabled=1 AND b.activity='idle' AND b.thread_id=?", (agent,thread)).fetchone()
                if not valid:
                    db.execute("UPDATE desktop_wake_deliveries SET state='CANCELLED',detail='Recipient active, unknown, retired, disconnected or rebound before submission' WHERE token=?", (token,))
                    continue
            try:
                detail = self.runner(thread, prompt)
                state = 'DELIVERED'
            except Exception as exc:
                detail, state = str(exc)[:500], 'UNCERTAIN'
            with self.hub.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('UPDATE desktop_wake_deliveries SET state=?,detail=? WHERE token=?',
                           (state, detail, token))
                if state == 'DELIVERED':
                    db.execute('UPDATE desktop_wake_bindings SET cursor=MAX(cursor,?),'
                               'partial_message=0,partial_offset=0 WHERE agent=? AND thread_id=?',
                               (through, agent, thread))
        return len(requests)

    def observe_events(self):
        """Service all existing board event sources while agents are idle."""
        from . import coordination, file_lifecycle, patch_queue, waits
        coordination.scan(self.hub)
        patch_queue.finalize_pending(self.hub)
        with self.hub.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            file_lifecycle.reconcile(self.hub, db, 'system', now)
            expired_jobs = list(db.execute("SELECT id,owner FROM jobs WHERE state='CLAIMED' AND expires<=?", (now,)))
            expired_claims = list(db.execute('SELECT id,agent FROM claims WHERE active=1 AND expires<=?', (now,)))
            self.hub.expire_leases(db, now)
            affected = {row['owner'] for row in expired_jobs} | {row['agent'] for row in expired_claims}
            for owner in affected:
                if not db.execute("SELECT 1 FROM agents WHERE id=? AND state='active' AND disconnected IS NULL", (owner,)).fetchone():
                    continue
                jobs = [row['id'] for row in expired_jobs if row['owner'] == owner]
                claims = [row['id'] for row in expired_claims if row['agent'] == owner]
                coordination.message(db, owner,
                    f'Swarmboard lease expiry: jobs {jobs}, claims {claims}. '
                    'Expired jobs are reopened and expired locks released. Check board_status and job_list '
                    'before continuing; reacquire required claims. Unfinished custody is preserved.', now)
            waits.process(db, now)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.once()
            except Exception as exc:
                print(f'Swarmboard queue worker: {exc}', flush=True)
            self.stop_event.wait(1)

    def start(self):
        self.recover()
        self.thread = threading.Thread(target=self.run, name='swarmboard-inbox-queue', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=35)
