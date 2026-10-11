import tempfile
import time
import unittest
from pathlib import Path
from board.core import BoardError, Hub
from board.desktop_wake import dispatch, reminder
from board.waits import poll
from board.desktop_wake import QueueWorker
from unittest.mock import patch


class DesktopWakeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.hub=Hub(self.root/'hub.sqlite',self.root)
        self.a=self.hub.call('register',{'name':'target'})
        self.b=self.hub.call('register',{'name':'relay'})

    def call(self,who,method,**args):
        return self.hub.call(method,{**args,'agent_id':who['agent_id'],'session_token':who['session_token']})

    def wake(self,who,action,**args):
        return self.call(who,'board_desktop_wake',action=action,**args)

    def enable(self,who=None,**args):
        return self.wake(who or self.a,'enable',thread_id=args.pop('thread_id','exact-chat'),workspace=str(self.root),owner_authorization='OWNER_REQUESTED_DESKTOP_WAKE',reason='Human requested desktop wake',**args)

    def send(self,body='The actual peer message is delivered verbatim'):
        return self.call(self.b,'inbox_send',recipient_agent_id=self.a['agent_id'],body=body)['id']

    def pending(self):
        return self.wake(self.b,'pending')['requests']

    def test_plain_message_one_way_and_no_ack(self):
        self.enable(); msg=self.send()
        one=self.pending()[0]
        self.assertEqual(one['thread_id'],'exact-chat')
        self.assertIn('The actual peer message is delivered verbatim',one['prompt'])
        self.assertNotIn('No reply',one['prompt'])
        self.assertIn(f'Message #{msg} from',one['prompt'])
        self.assertEqual(self.pending(),[])
        self.wake(self.b,'delivered',delivery_token=one['delivery_token'])
        self.assertEqual(self.pending(),[])
        self.assertEqual(self.call(self.a,'inbox_read')['messages'][0]['id'],msg)
        self.send('next event')
        self.assertEqual(len(self.pending()),1)

    def test_all_system_event_sources_batch_together(self):
        self.enable()
        message=self.call(self.a,'inbox_send',recipient_agent_id=self.b['agent_id'],body='Question')['id']
        self.call(self.a,'board_wait',action='subscribe',kind='message_ack',message_id=message)
        self.call(self.b,'inbox_ack',message_ids=[message])
        claim=self.call(self.b,'mutex_claim',resources=['db:work'],reason='busy')
        self.call(self.a,'board_wait',action='subscribe',kind='mutex_free',resource='db:work')
        self.call(self.b,'release',claim_ids=claim['claim_ids'],reason='done')
        file=self.root/'common.txt';file.write_text('base\n')
        batch=self.call(self.a,'patch_queue',action='join',path=str(file),reason='shared')['batch']['id']
        self.call(self.a,'patch_queue',action='release',batch_id=batch)
        self.call(self.b,'mutex_claim',resources=['db:timeout'],reason='busy')
        wait=self.call(self.a,'board_wait',action='subscribe',kind='mutex_free',resource='db:timeout')['wait']['id']
        with self.hub.connect() as db:
            db.execute('UPDATE board_waits SET expires=0 WHERE id=?',(wait,))
        poll(self.root/'hub.sqlite',self.root)
        import hashlib
        before=hashlib.sha256(file.read_bytes()).hexdigest()
        file.write_text('changed\n')
        after=hashlib.sha256(file.read_bytes()).hexdigest()
        self.call(self.b,'board_coordination',action='report',path=str(file),reason='test incident',before_hash=before,after_hash=after)
        request=self.pending()[0]
        self.assertEqual(request['unread_count'],5)
        self.assertEqual(len(self.call(self.a,'inbox_read')['messages']),5)

    def test_long_real_message_delivers_every_character_across_restart(self):
        self.enable()
        body='quote" slash\\ newline\n' * 650
        self.send(body)
        fragments=[]
        for _ in range(30):
            requests=self.pending()
            if not requests: break
            request=requests[0]
            payload=request['prompt'].split(':\n',1)[1]
            if '\n[Continues in the next delivery;' in payload:
                payload=payload.split('\n[Continues in the next delivery;',1)[0]
            else:
                payload=payload[:-1]
            fragments.append(payload)
            self.wake(self.b,'delivered',delivery_token=request['delivery_token'])
            self.hub=Hub(self.root/'hub.sqlite',self.root)
        self.assertEqual(''.join(fragments),body)
        self.assertEqual(len(self.call(self.a,'inbox_read')['messages']),1)

    def test_uncertainty_survives_restart_and_other_relay_cannot_resolve(self):
        self.enable();self.send()
        delivery=self.pending()[0]
        with self.assertRaisesRegex(BoardError,'reserving relay'):
            self.wake(self.a,'delivered',delivery_token=delivery['delivery_token'])
        self.wake(self.b,'failed',delivery_token=delivery['delivery_token'],outcome='uncertain')
        self.hub=Hub(self.root/'hub.sqlite',self.root)
        self.assertEqual(self.pending(),[])
        self.wake(self.b,'delivered',delivery_token=delivery['delivery_token'])
        self.assertEqual(self.pending(),[])

    def test_known_not_sent_can_retry_and_receipt_is_idempotent(self):
        self.enable();self.send()
        delivery=self.pending()[0]
        self.wake(self.b,'failed',delivery_token=delivery['delivery_token'],outcome='not_sent')
        retry=self.pending()[0]
        self.wake(self.b,'delivered',delivery_token=retry['delivery_token'])
        self.wake(self.b,'delivered',delivery_token=retry['delivery_token'])
        self.assertEqual(self.pending(),[])

    def test_backlog_opt_in_and_repeat_enable_does_not_rebaseline(self):
        self.send();self.enable()
        self.assertEqual(self.pending(),[])
        self.send();self.enable()
        self.assertEqual(len(self.pending()),1)

    def test_disconnect_and_retirement_stop_delivery(self):
        self.enable(); self.send()
        self.call(self.a,'disconnect',reason='closed')
        self.assertEqual(self.pending(),[])
        self.call(self.a,'board_status',target_agent_id='self',include_rules=False)
        self.call(self.b,'retire',target_agent_id=self.a['agent_id'],reason='owner requested retirement')
        self.assertEqual(self.pending(),[])

    def test_binding_requires_exact_workspace_and_live_owner(self):
        with self.assertRaisesRegex(BoardError,'workspace mismatch'):
            self.wake(self.a,'enable',thread_id='exact-chat',workspace=str(self.root/'wrong'),reason='human',owner_authorization='OWNER_REQUESTED_DESKTOP_WAKE')
        self.enable()
        with self.assertRaisesRegex(BoardError,'another live agent'):
            self.enable(self.b)
        self.send()
        with self.hub.connect() as db:
            self.assertEqual(reminder(db,self.b['agent_id']),{})
            self.assertEqual(reminder(db,self.a['agent_id']),{})

    def test_automatic_registration_needs_no_permission_marker(self):
        self.assertTrue(self.wake(self.a, 'status')['auto_enable_required'])
        self.wake(self.a, 'enable', thread_id='exact-chat', workspace=str(self.root), reason='Automatic registration')
        self.hub = Hub(self.root/'hub.sqlite', self.root)
        self.assertFalse(self.wake(self.a, 'status')['auto_enable_required'])
        self.send()
        self.assertEqual(len(self.pending()), 1)

    def test_old_optout_settings_do_not_disable_service(self):
        with self.hub.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('desktop_wake_default','0')")
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('desktop_wake_optout:' + self.a['agent_id'], '1'))
        status = self.wake(self.a, 'status')
        self.assertTrue(status['default_enabled'])
        self.assertTrue(status['auto_enable_required'])
        for action in ('disable', 'defaults'):
            with self.assertRaisesRegex(BoardError, 'always enabled'):
                self.wake(self.a, action, enabled=False)

    def worker(self, runner=None):
        self.submissions = []
        return QueueWorker(self.hub, runner or (lambda thread, prompt: self.submissions.append((thread, prompt)) or 'Queued message test'))

    def test_queue_only_new_inbox_arrivals_not_checks_notices_or_unacked_messages(self):
        self.enable(); worker = self.worker()
        for _ in range(3):
            self.call(self.a, 'board_status', target_agent_id='self', include_rules=False)
            self.call(self.b, 'noticeboard_post', body='General information')
            self.assertEqual(worker.once(), 0)
        first = self.send()
        self.assertEqual(worker.once(), 1)
        self.assertEqual(self.submissions[0][0], 'exact-chat')
        self.assertEqual(self.call(self.a, 'inbox_read')['messages'][0]['id'], first)
        for _ in range(3):
            self.assertEqual(worker.once(), 0)
        self.assertEqual(self.wake(self.a, 'status')['last_delivery']['state'], 'DELIVERED')
        self.send('Second arrival')
        self.assertEqual(worker.once(), 1)
        self.assertEqual(len(self.submissions), 2)

    def test_queue_batches_long_messages_without_chunk_wakes_and_survives_restart(self):
        self.enable(); worker = self.worker()
        for _ in range(4):
            self.send('large body ' * 1000)
        self.assertEqual(worker.once(), 1)
        self.assertLess(len(self.submissions[0][1]), 1000)
        self.hub = Hub(self.root/'hub.sqlite', self.root)
        restarted = self.worker(); restarted.recover()
        self.assertEqual(restarted.once(), 0)
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages WHERE recipient=? AND acknowledged IS NULL', (self.a['agent_id'],)).fetchone()[0], 4)

    def test_concurrent_worker_cannot_reserve_same_inbox_batch(self):
        self.enable(); self.send()
        other = QueueWorker(self.hub, lambda *args: self.fail('duplicate submission'))
        worker = self.worker(lambda *args: self.assertEqual(other.once(), 0) or 'Queued')
        self.assertEqual(worker.once(), 1)
        self.assertEqual(other.once(), 0)

    def test_failed_or_timed_out_queue_does_not_spam_retry_even_after_restart(self):
        self.enable(); self.send()
        attempts = []
        def fail(*args):
            attempts.append(args)
            raise TimeoutError('unknown queue result')
        worker = self.worker(fail)
        self.assertEqual(worker.once(), 1)
        self.send('Another arrival while uncertain')
        self.assertEqual(worker.once(), 0)
        self.hub = Hub(self.root/'hub.sqlite', self.root)
        restarted = self.worker(fail); restarted.recover()
        self.assertEqual(restarted.once(), 0)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.wake(self.a, 'status')['last_delivery']['state'], 'UNCERTAIN')

    def test_crash_after_submission_before_receipt_is_not_resent(self):
        self.enable(); self.send()
        with self.hub.connect() as db:
            db.execute("INSERT INTO desktop_wake_deliveries(token,agent,thread_id,relay,through_id,state,created) VALUES (?,?,?,?,1,'IN_FLIGHT',?)",
                       ('interrupted', self.a['agent_id'], 'exact-chat', QueueWorker.RELAY, time.time()))
        worker = self.worker(); worker.recover()
        self.assertEqual(worker.once(), 0)
        self.assertEqual(self.wake(self.a, 'status')['last_delivery']['state'], 'UNCERTAIN')

    def test_arrival_during_submission_gets_one_later_wake(self):
        self.enable(); self.send()
        worker = self.worker(lambda *args: self.send('During delivery') and 'Queued')
        self.assertEqual(worker.once(), 1)
        worker.runner = lambda *args: 'Queued'
        self.assertEqual(worker.once(), 1)
        self.assertEqual(worker.once(), 0)

    def test_queue_submit_uses_fixed_argument_vector_without_shell(self):
        with patch('board.desktop_wake.codex_executable', return_value='codex.exe'), patch('board.desktop_wake.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = 'Queued'
            QueueWorker.submit('exact-chat', 'Quotes " and $(shell) are data')
            self.assertEqual(run.call_args.args[0], ['codex.exe', 'queue', '--thread', 'exact-chat', '--message', 'Quotes " and $(shell) are data'])
            self.assertFalse(run.call_args.kwargs['shell'])

    def test_three_same_harness_chats_queue_only_exact_recipient(self):
        self.enable(thread_id='chat-one')
        self.enable(self.b, thread_id='chat-two')
        third = self.hub.call('register', {'name': 'target'})
        self.enable(third, thread_id='chat-three')
        self.call(self.a, 'inbox_send', recipient_agent_id=self.b['agent_id'], body='Only chat two')
        worker = self.worker()
        self.assertEqual(worker.once(), 1)
        self.assertEqual(self.submissions[0][0], 'chat-two')
        self.assertEqual(worker.once(), 0)


if __name__=='__main__':
    unittest.main()
