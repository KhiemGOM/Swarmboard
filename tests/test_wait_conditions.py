import json
import tempfile
import unittest
from pathlib import Path
from board.core import Hub, BoardError
from board.desktop_wake import QueueWorker


class ConditionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(self.root/'hub.sqlite', self.root)
        self.a = self.hub.call('register', {'name':'waiter'})
        self.b = self.hub.call('register', {'name':'holder'})

    def call(self, who, method, **args):
        return self.hub.call(method, {**who, **args})

    def subscribe(self, condition, **args):
        return self.call(self.a, 'board_wait', action='subscribe', kind='condition', condition=condition, **args)['wait']

    def lock(self, resource, who=None):
        return self.call(who or self.b,'mutex_claim',resources=[resource],reason='work')['claim_ids']

    def release(self, ids, who=None):
        return self.call(who or self.b,'release',claim_ids=ids,reason='done',held_reason='Test work retained')

    def messages(self):
        return self.call(self.a,'inbox_read')['messages']

    def test_all_lock_types_and_queue_count_once(self):
        resource = self.lock('db:test')
        file = self.call(self.b,'claim',paths=['direct.py'],reason='work')['claim_ids']
        path=self.root/'shared.py'; path.write_text('base\n',encoding='utf-8')
        batch=self.call(self.b,'patch_queue',action='join',path=str(path),reason='shared')['batch']['id']
        self.call(self.a,'patch_queue',action='join',path=str(path),reason='shared')
        wait=self.subscribe({'kind':'locks','remaining_lte':2})
        self.assertEqual(wait['state'],'WAITING')  # 2 claims + 1 batch, not 2 members.
        self.release(resource)
        self.assertEqual(len(self.messages()),1)
        self.release(file)
        self.assertEqual(len(self.messages()),1)

    def test_all_but_one_and_excluded_resource(self):
        one=self.lock('git:index'); two=self.lock('db:test'); three=self.lock('port:1234')
        self.subscribe({'kind':'locks','exclude_resources':['git:index'],'remaining_lte':0})
        self.release(two); self.assertEqual(self.messages(),[])
        self.release(three); self.assertEqual(len(self.messages()),1)
        self.subscribe({'kind':'locks','remaining_lte':1})
        self.assertEqual(len(self.messages()),2)
        self.release(one); self.assertEqual(len(self.messages()),2)

    def test_snapshot_ignores_new_locks_dynamic_does_not(self):
        old=self.lock('db:old')
        snap=self.subscribe({'kind':'locks','scope':'snapshot'})
        dynamic=self.subscribe({'kind':'locks'})
        self.assertEqual(json.loads(snap['target'])['claim_ids'],old)
        new=self.lock('db:new'); self.release(old)
        self.assertEqual(len(self.messages()),1)
        self.release(new); self.assertEqual(len(self.messages()),2)
        self.assertNotEqual(snap['id'],dynamic['id'])

    def test_path_owner_and_expiry_worker_without_model_poll(self):
        held=self.call(self.b,'claim',paths=['src/a.py'],reason='work')['claim_ids']
        self.lock('db:other')
        self.subscribe({'kind':'locks','path':str(self.root/'src'),'owner':self.b['agent_id']})
        with self.hub.connect() as db:
            db.execute('UPDATE claims SET expires=0 WHERE id=?',(held[0],))
        QueueWorker(self.hub,lambda *args: 'queued').once()
        self.assertEqual(len(self.messages()),1)

    def test_all_any_ack_job_and_patch_leaves(self):
        lock=self.lock('db:test')
        msg=self.call(self.a,'inbox_send',recipient_agent_id=self.b['agent_id'],body='hello')['id']
        job=self.call(self.b,'job_post',title='job',description='work',acceptance='done')['job']['id']
        condition={'all':[{'kind':'message_ack','message_id':msg}, {'any':[
            {'kind':'locks','resources':['db:test']},{'kind':'job_state','job_id':job,'state':'CLAIMED'}]}]}
        self.subscribe(condition)
        self.call(self.b,'inbox_ack',message_ids=[msg]); self.assertEqual(self.messages(),[])
        self.call(self.b,'job_claim',job_id=job); self.assertEqual(len(self.messages()),1)

    def test_patch_completion_composes_with_resource_availability(self):
        path=self.root/'shared.py'; path.write_text('base\n',encoding='utf-8')
        batch=self.call(self.b,'patch_queue',action='join',path=str(path),reason='shared')['batch']['id']
        held=self.lock('git:index')
        self.subscribe({'all':[{'kind':'patch_finalized','batch_id':batch},
            {'kind':'locks','resources':['git:index']}]})
        self.call(self.b,'patch_queue',action='release',batch_id=batch)
        self.assertEqual(self.messages(),[])
        self.release(held)
        self.assertEqual(len(self.messages()),1)

    def test_timeout_cancel_restart_and_dedup(self):
        self.lock('db:test')
        condition={'kind':'locks'}
        wait=self.subscribe(condition)
        self.assertEqual(self.subscribe(condition)['id'],wait['id'])
        self.hub=Hub(self.root/'hub.sqlite',self.root)
        self.call(self.a,'board_wait',action='cancel',wait_id=wait['id'])
        wait=self.subscribe(condition)
        with self.hub.connect() as db:
            db.execute('UPDATE board_waits SET expires=0 WHERE id=?',(wait['id'],))
        QueueWorker(self.hub,lambda *args:'queued').once()
        self.assertIn('timed out',self.messages()[0]['body'])
        self.assertEqual(len(self.messages()),1)

    def test_self_filter_follows_inherited_wait_without_premature_fire(self):
        held=self.lock('db:self',self.a)
        self.subscribe({'kind':'locks','owner':'self'})
        self.call(self.b,'inherit',target_agent_id=self.a['agent_id'],inheritance_secret=self.a['inheritance_secret'],reason='continue')
        self.assertEqual(self.call(self.b,'inbox_read')['messages'],[])
        self.release(held,self.b)
        self.assertEqual(len(self.call(self.b,'inbox_read')['messages']),1)

    def test_validation_and_ack_ownership(self):
        foreign=self.call(self.b,'inbox_send',recipient_agent_id=self.a['agent_id'],body='x')['id']
        cases=[{}, {'all':[]},{'all':[{'kind':'locks'}],'any':[]},
            {'kind':'locks','remaining_lte':True},{'kind':'locks','remaining_lte':-1},
            {'kind':'locks','scope':'oops'},{'kind':'locks','owner':'missing'},
            {'kind':'locks','claim_ids':[999999]},{'kind':'locks','lock_types':[{}]},
            {'kind':'locks','path':str(self.root.parent/'outside')},
            {'kind':'message_ack','message_id':foreign},{'kind':'locks','oops':0}]
        for condition in cases:
            with self.subTest(condition=condition),self.assertRaises(BoardError): self.subscribe(condition)
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM board_waits').fetchone()[0],0)
