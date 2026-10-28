#!/usr/bin/env python3
"""Temporary journal/lock/CAS/crash fixtures; no real worker, credentials or network."""
from pathlib import Path
import copy
import hashlib
import io
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
import bus_service as service
REAL_CONSUMER_PROOF=service.assert_consumer_proof


def hold_lock(root,record,ready,release):
    with service.lifecycle_lock(root,*service._actor(record)):
        ready.set(); release.wait(5)


class Crash(BaseException): pass


class Retirement(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.comm=Path(self.temp.name).resolve()/'comm';self.root=self.comm/'bus';self.root.mkdir(parents=True,mode=0o700)
        self.record={'kind':'service','url':'https://broker.invalid','id':'a_'+'1'*32,'service_id':str(uuid4()),'buses':['general']}
        self.proof={'proof_sha256':'a'*64,'worker_runtime':'b'*64,'source_sha256':{'lib/bus.py':'c'*64,'lib/bus_service.py':'d'*64},
                    'old_consumer_pid':1234567,'current_pid':1234568,'current_started_at':1234.5}
        self.proof_patch=mock.patch.object(service,'assert_consumer_proof',return_value=self.proof)
        self.proof_patch.start();self.addCleanup(self.proof_patch.stop)

    def message(self,name='one',epoch=None):
        envelope={'id':'m_'+name,'target':self.record['id'],'bus':'general','reply_to':'m_'+name,'message':'data',
                  'created_at':time.time(),'expires_at':time.time()+300,'conversation_expires_at':time.time()+500,
                  'sender':{'id':'a_'+'2'*32,'kind':'codex','user':None,'device_id':'p_'+'3'*32},
                  'correlation_version':1,'conversation_id':'c_'+'4'*32,'in_reply_to':None}
        if epoch is not None:envelope.update(protocol=service.PROTOCOL,recipient_epoch=epoch);envelope['sender']['epoch']=None
        return envelope

    def receipt(self,expected=0,key='retire-1'):
        request={'op':'service_fence_retire','agent':self.record['id'],'service_id':self.record['service_id'],
                 'expected_epoch':expected,'request_id':key}
        return {'protocol':service.PROTOCOL,'agent':self.record['id'],'service_id':self.record['service_id'],
                'previous_epoch':expected,'epoch':expected+1,'retired_epoch':expected+1,'retired':True,
                'request_id':key,'payload_hash':service._hash(request),'principal':'p_'+'3'*32,'server_id':'server-fixture'}

    def retire(self,expected=0,key='retire-1'):
        with service.retirement_barrier(self.root,self.record,expected_epoch=expected,request_id=key):
            return service.complete_retirement(self.root,self.record,self.receipt(expected,key),expected_epoch=expected,request_id=key)

    def row(self,name='one'):
        db=service.journal(self.root)
        try:return dict(db.execute('SELECT * FROM jobs WHERE id=?',('m_'+name,)).fetchone())
        finally:db.close()

    def test_legacy_unknown_epoch_is_preserved_as_abandoned_not_consumed(self):
        service.enqueue(self.root,self.record,self.message());original=self.row()
        evidence=self.retire();row=self.row()
        self.assertEqual(row['state'],'abandoned_retired');self.assertIsNone(row['receipt']);self.assertIsNone(row['recipient_epoch'])
        for field in ['envelope','request_hash','reply','receipt','sender_epoch','recipient_epoch']:self.assertEqual(row[field],original[field])
        self.assertFalse(evidence['native_journal_drained']);self.assertFalse(evidence['consumption_claimed'])
        self.assertEqual(service.retirement_evidence(self.root,self.record,evidence['barrier_id']),evidence)
        called=[];service.advance(self.root,{'r':self.record},{self.record['url']:{}},lambda *a,**k:called.append('broker'),call_bridge=lambda *a:called.append('endpoint'))
        self.assertEqual(called,[])
        with self.assertRaises(service.ServiceError):service.enqueue(self.root,self.record,self.message('late'))

    def test_reactivation_accepts_only_new_generation_and_preserves_v2_acknowledgment(self):
        service.enqueue(self.root,self.record,self.message());evidence=self.retire()
        active={**self.record,'fence_protocol':service.PROTOCOL,'epoch':2,'contract_version':2};service.activate_generation(self.root,active)
        for epoch in [None,0,1]:
            with self.subTest(epoch=epoch),self.assertRaises(service.ServiceError):service.enqueue(self.root,active,self.message('stale'+str(epoch),epoch))
        service.enqueue(self.root,active,self.message('new',2))
        service.advance(self.root,{'r':active},{active['url']:{}},lambda *a,**k:self.fail('acknowledgment must not reply'),call_bridge=lambda *a:{'pending':False,'disposition':'acknowledge'})
        self.assertEqual(self.row('new')['state'],'acknowledged');self.assertEqual(self.row()['state'],'abandoned_retired')
        with self.assertRaises(service.ServiceError):service.retirement_evidence(self.root,self.record,evidence['barrier_id'])

    def test_callback_race_blocks_reply_and_records_late_observation(self):
        service.enqueue(self.root,self.record,self.message());entered=threading.Event();release=threading.Event();completed=threading.Event();posts=[]
        def callback(*a):entered.set();self.assertTrue(release.wait(3));return {'pending':False,'reply':'late actual answer'}
        work=threading.Thread(target=lambda:service.advance(self.root,{'r':self.record},{self.record['url']:{}},lambda *a,**k:posts.append(k),call_bridge=callback));work.start()
        self.assertTrue(entered.wait(3));service.quiesce(self.root,self.record,expected_epoch=0,request_id='retire-1')
        result=[]
        def retiring():result.append(service.complete_retirement(self.root,self.record,self.receipt(),expected_epoch=0,request_id='retire-1'));completed.set()
        retire=threading.Thread(target=retiring);retire.start();self.assertFalse(completed.wait(.05))
        release.set();work.join(3);retire.join(3);self.assertTrue(completed.is_set());self.assertEqual(posts,[])
        self.assertEqual(self.row()['state'],'abandoned_retired');self.assertIsNone(self.row()['reply'])
        db=service.journal(self.root)
        try:self.assertEqual(db.execute('SELECT kind FROM service_job_observations').fetchone()[0],'callback_after_quiesce')
        finally:db.close()

    def test_broker_reply_race_cannot_mark_sent_after_quiesce(self):
        service.enqueue(self.root,self.record,self.message());entered=threading.Event();release=threading.Event()
        def broker(*a,**k):entered.set();self.assertTrue(release.wait(3));return {'id':'m_committed_old'}
        work=threading.Thread(target=lambda:service.advance(self.root,{'r':self.record},{self.record['url']:{}},broker,call_bridge=lambda *a:{'pending':False,'reply':'answer'}));work.start()
        self.assertTrue(entered.wait(3));service.quiesce(self.root,self.record,expected_epoch=0,request_id='retire-1');release.set();work.join(3)
        evidence=service.complete_retirement(self.root,self.record,self.receipt(),expected_epoch=0,request_id='retire-1')
        self.assertEqual(self.row()['state'],'abandoned_retired');self.assertIsNone(self.row()['receipt'])
        db=service.journal(self.root)
        try:
            observed=db.execute('SELECT kind,result FROM service_job_observations').fetchone()
            self.assertEqual(observed['kind'],'broker_reply_after_quiesce');self.assertEqual(json.loads(observed['result']),{'id':'m_committed_old'})
        finally:db.close()
        self.assertEqual(service.retirement_evidence(self.root,self.record,evidence['barrier_id']),evidence)

    def test_receipt_crash_preserves_durable_confirmation_and_resumes_same_intent(self):
        service.enqueue(self.root,self.record,self.message());service.quiesce(self.root,self.record,expected_epoch=0,request_id='retire-1')
        with mock.patch.object(service,'_snapshot_jobs',side_effect=Crash):
            with self.assertRaises(Crash):service.complete_retirement(self.root,self.record,self.receipt(),expected_epoch=0,request_id='retire-1')
        status=service.lifecycle_status(self.root,self.record)
        self.assertEqual(json.loads(status['receipt']),self.receipt());self.assertEqual(self.row()['state'],'pending')
        evidence=service.complete_retirement(self.root,self.record,self.receipt(),expected_epoch=0,request_id='retire-1')
        self.assertEqual(service.complete_retirement(self.root,self.record,self.receipt(),expected_epoch=0,request_id='retire-1'),evidence)
        db=service.journal(self.root)
        try:self.assertEqual(db.execute('SELECT count(*) FROM service_retirement_barriers').fetchone()[0],1)
        finally:db.close()
        with self.assertRaises(service.ServiceError):service.quiesce(self.root,self.record,expected_epoch=0,request_id='changed')

    def test_bad_receipts_do_not_abandon_and_newer_jobs_refuse(self):
        service.enqueue(self.root,self.record,self.message());service.quiesce(self.root,self.record,expected_epoch=0,request_id='retire-1')
        for key,value in [('agent','a_'+'f'*32),('service_id',str(uuid4())),('payload_hash','f'*64),('previous_epoch',True),('epoch',True),('retired',False),('historical_replay',True)]:
            bad={**self.receipt(),key:value}
            with self.subTest(key=key),self.assertRaises(service.ServiceError):service.complete_retirement(self.root,self.record,bad,expected_epoch=0,request_id='retire-1')
        self.assertEqual(self.row()['state'],'pending')
        db=service.journal(self.root)
        with db:db.execute('UPDATE jobs SET recipient_epoch=2')
        db.close()
        with self.assertRaises(service.ServiceError):service.complete_retirement(self.root,self.record,self.receipt(),expected_epoch=0,request_id='retire-1')
        self.assertEqual(service.lifecycle_status(self.root,self.record)['phase'],'quiescing')

    def test_process_shared_lock_timeout_and_reentrancy(self):
        ctx=multiprocessing.get_context('spawn');ready=ctx.Event();release=ctx.Event()
        child=ctx.Process(target=hold_lock,args=(str(self.root),self.record,ready,release));child.start()
        try:
            self.assertTrue(ready.wait(5))
            with self.assertRaises(service.ServiceError):
                with service.lifecycle_lock(self.root,*service._actor(self.record),timeout_s=.05):pass
        finally:release.set();child.join(5)
        self.assertEqual(child.exitcode,0)
        with service.lifecycle_lock(self.root,*service._actor(self.record)):
            with service.lifecycle_lock(self.root,*service._actor(self.record)):pass

    def test_quiesce_lock_timeout_stays_blocked(self):
        ctx=multiprocessing.get_context('spawn');ready=ctx.Event();release=ctx.Event()
        child=ctx.Process(target=hold_lock,args=(str(self.root),self.record,ready,release));child.start()
        try:
            self.assertTrue(ready.wait(5))
            with self.assertRaises(service.ServiceError):
                with service.retirement_barrier(self.root,self.record,expected_epoch=0,request_id='retire-1',timeout_s=.05):pass
            self.assertEqual(service.lifecycle_status(self.root,self.record)['phase'],'quiescing')
        finally:release.set();child.join(5)

    def test_guard_reopens_actual_barrier_and_holds_lock_through_caller_cas(self):
        evidence=self.retire();scope={'hub':self.record['url'],'principal':'p_'+'3'*32,'comm_state':str(self.comm)}
        ref={'role':'worker','service_id':self.record['service_id'],'registration_id':self.record['id'],'slot_fence':1,
             'claim_sha256':'d'*64,'barrier_id':evidence['barrier_id'],'barrier_sha256':evidence['evidence_sha256'],'previous_epoch':0,'retired_epoch':1}
        with service.guard_completed_retirement_barriers(self.root,scope,[ref]) as snapshot:
            self.assertEqual(snapshot['actors'][0]['phase'],'retired');self.assertEqual(snapshot['actors'][0]['callbacks_inflight'],0)
            failures=[]
            def contend():
                try:
                    with service.lifecycle_lock(self.root,*service._actor(self.record),timeout_s=.05):pass
                except service.ServiceError:failures.append(True)
            thread=threading.Thread(target=contend);thread.start();thread.join(2);self.assertEqual(failures,[True])
        for bad in [{**ref,'barrier_sha256':'0'*64},{**ref,'retired_epoch':3}]:
            with self.assertRaises(service.ServiceError):
                with service.guard_completed_retirement_barriers(self.root,scope,[bad]):pass
        with self.assertRaises(service.ServiceError):
            with service.guard_completed_retirement_barriers(self.root,{**scope,'principal':'p_'+'f'*32},[ref]):pass

    def test_unknown_lifecycle_and_lock_symlink_refuse(self):
        with self.assertRaises(service.ServiceError):service.activate_generation(self.root,{**self.record,'fence_protocol':service.PROTOCOL,'epoch':2})
        directory=self.root/'service-lifecycle-locks';directory.mkdir(mode=0o700,exist_ok=True)
        key=service._hash([str(self.root),*service._actor(self.record)])
        target=self.root/'other';target.write_text('');target.chmod(0o600)
        lockpath=directory/(key+'.lock');lockpath.unlink(missing_ok=True);lockpath.symlink_to(target)
        with self.assertRaises((service.ServiceError,OSError)):
            with service.lifecycle_lock(self.root,*service._actor(self.record)):pass

    def test_actual_consumer_validator_fails_closed_without_proof(self):
        with self.assertRaises((OSError,service.ServiceError)):REAL_CONSUMER_PROOF(self.root)
        old=1234567;now=time.time();source=Path(service.__file__);bus=source.with_name('bus.py')
        runtime=hashlib.sha256(bus.read_bytes()+source.read_bytes()).hexdigest()
        identity=service.consumer_process_identity(os.getpid())
        proof={'schema':'communicate.service-consumer-proof/v1','root':str(self.root),'old_consumer_pid':old,
               'current_pid':os.getpid(),'current_started_at':now,'worker_runtime':runtime,
               'process_identity':identity,'comm_state':str(self.comm),
               'source_sha256':{'lib/bus.py':hashlib.sha256(bus.read_bytes()).hexdigest(),'lib/bus_service.py':hashlib.sha256(source.read_bytes()).hexdigest()}}
        worker={'pid':os.getpid(),'runtime':runtime,'started_at':now,'state':'running','heartbeat_at':now,
                'process_identity':identity,'comm_state':str(self.comm)}
        for name,value in [('service-consumer-proof.json',proof),('worker.json',worker)]:
            path=self.root/name;path.write_text(json.dumps(value));path.chmod(0o600)
        def kill(pid,signal):
            if pid==old:raise ProcessLookupError
        observed=subprocess.CompletedProcess([],0,str(os.getuid())+' python '+str(bus.resolve())+' __worker --comm-state '+str(self.comm),'')
        with mock.patch.object(service.os,'kill',side_effect=kill),mock.patch.object(service.subprocess,'run',return_value=observed):
            result=REAL_CONSUMER_PROOF(self.root)
            self.assertEqual(result['worker_runtime'],runtime);self.assertEqual(result['process_identity'],identity)
            self.assertEqual(result['comm_state'],str(self.comm))
        with mock.patch.object(service.os,'kill',return_value=None),self.assertRaises(service.ServiceError):REAL_CONSUMER_PROOF(self.root)
        with mock.patch.object(service.os,'kill',side_effect=PermissionError),self.assertRaises(service.ServiceError):REAL_CONSUMER_PROOF(self.root)

    def test_fenced_source_can_deliver_to_legacy_receiver_without_recipient_epoch(self):
        message=self.message('mixed');message.update(protocol=service.PROTOCOL,recipient_epoch=None)
        message['sender']['epoch']=2
        service.enqueue(self.root,self.record,message)
        self.assertEqual(self.row('mixed')['sender_epoch'],2);self.assertIsNone(self.row('mixed')['recipient_epoch'])
        replies=[]
        service.advance(self.root,{'r':self.record},{self.record['url']:{}},lambda *a,**kw:replies.append(kw) or {'id':'m_reply'},
                        call_bridge=lambda *a:{'pending':False,'reply':'answer'})
        self.assertEqual(self.row('mixed')['state'],'sent');self.assertNotIn('expected_sender_epoch',replies[0])

    def test_current_fenced_reply_carries_exact_sender_epoch(self):
        self.retire();active={**self.record,'fence_protocol':service.PROTOCOL,'epoch':2};service.activate_generation(self.root,active)
        service.enqueue(self.root,active,self.message('new',2));replies=[]
        service.advance(self.root,{'r':active},{active['url']:{}},lambda *a,**kw:replies.append(kw) or {'id':'m_reply'},
                        call_bridge=lambda *a:{'pending':False,'reply':'answer'})
        self.assertEqual(replies[0]['expected_sender_epoch'],2);self.assertEqual(self.row('new')['state'],'sent')

    def test_acknowledged_callback_after_quiesce_is_only_an_observation(self):
        self.record['contract_version']=2
        service.enqueue(self.root,self.record,self.message())
        def callback(*a):
            service.quiesce(self.root,self.record,expected_epoch=0,request_id='retire-1')
            return {'pending':False,'disposition':'acknowledge'}
        service.advance(self.root,{'r':self.record},{self.record['url']:{}},lambda *a,**k:self.fail('no reply'),call_bridge=callback)
        self.assertEqual(self.row()['state'],'pending')
        service.complete_retirement(self.root,self.record,self.receipt(),expected_epoch=0,request_id='retire-1')
        self.assertEqual(self.row()['state'],'abandoned_retired')


    def test_kernel_start_token_and_exact_scope_refuse_stale_same_source_before_broker(self):
        old=1234567;now=time.time();source=Path(service.__file__);bus=source.with_name('bus.py')
        identity={'kind':'macos-libproc-start/v1','pid':os.getpid(),'start_sec':1700000000,'start_usec':42}
        runtime=hashlib.sha256(bus.read_bytes()+source.read_bytes()).hexdigest()
        proof={'schema':'communicate.service-consumer-proof/v1','root':str(self.root),'old_consumer_pid':old,
               'current_pid':os.getpid(),'current_started_at':now,'worker_runtime':runtime,
               'process_identity':identity,'comm_state':str(self.comm),
               'source_sha256':{'lib/bus.py':hashlib.sha256(bus.read_bytes()).hexdigest(),'lib/bus_service.py':hashlib.sha256(source.read_bytes()).hexdigest()}}
        worker={'pid':os.getpid(),'runtime':runtime,'started_at':now,'state':'running','heartbeat_at':now,
                'process_identity':identity,'comm_state':str(self.comm)}
        def write(p,w):
            for name,value in [('service-consumer-proof.json',p),('worker.json',w)]:
                path=self.root/name;path.write_text(json.dumps(value));path.chmod(0o600)
        def kill(pid,signal):
            if pid==old:raise ProcessLookupError
        observed=subprocess.CompletedProcess([],0,str(os.getuid())+' python '+str(bus.resolve())+' __worker --comm-state '+str(self.comm),'')
        # Same PID/source/fresh heartbeat/start metadata; actual kernel says this
        # PID is a newer process. Other cases model a same-source wrong scope,
        # missing token and Python equality traps, none reaching broker HTTP.
        cases=[(proof,worker,{**identity,'start_usec':43}),
               (proof,{**worker,'comm_state':str(self.comm/'other')},identity),
               ({**proof,'comm_state':str(self.comm/'other')},worker,identity),
               (proof,{key:value for key,value in worker.items() if key!='process_identity'},identity),
               ({key:value for key,value in proof.items() if key!='process_identity'},worker,identity),
               ({**proof,'process_identity':{**identity,'pid':float(os.getpid())}},worker,identity)]
        for p,w,actual in cases:
            with self.subTest(proof=p,worker=w,actual=actual):
                write(p,w);effects=[]
                with mock.patch.object(service,'assert_consumer_proof',side_effect=REAL_CONSUMER_PROOF),\
                     mock.patch.object(service,'consumer_process_identity',return_value=actual),\
                     mock.patch.object(service.os,'kill',side_effect=kill),\
                     mock.patch.object(service.subprocess,'run',return_value=observed),self.assertRaises(service.ServiceError):
                    with service.retirement_barrier(self.root,self.record,expected_epoch=0,request_id='blocked'):
                        effects.append('broker')
                self.assertEqual(effects,[]);self.assertIsNone(service.lifecycle_status(self.root,self.record))
        write(proof,worker)
        for suffix in ('',' --comm-state '+str(self.comm/'other')):
            wrong=subprocess.CompletedProcess([],0,str(os.getuid())+' python '+str(bus.resolve())+' __worker'+suffix,'')
            with self.subTest(argv=suffix),mock.patch.object(service,'consumer_process_identity',return_value=identity),\
                 mock.patch.object(service.os,'kill',side_effect=kill),mock.patch.object(service.subprocess,'run',return_value=wrong),\
                 self.assertRaises(service.ServiceError):REAL_CONSUMER_PROOF(self.root)
        with mock.patch.object(service,'consumer_process_identity',side_effect=[identity,{**identity,'start_usec':44}]),\
             mock.patch.object(service.os,'kill',side_effect=kill),mock.patch.object(service.subprocess,'run',return_value=observed),\
             self.assertRaises(service.ServiceError):REAL_CONSUMER_PROOF(self.root)
        with mock.patch.object(service,'consumer_process_identity',side_effect=service.ServiceError('unsupported')),\
             mock.patch.object(service.os,'kill',side_effect=kill),self.assertRaises(service.ServiceError):REAL_CONSUMER_PROOF(self.root)


    def test_kernel_process_token_is_stable_and_uses_platform_kernel_fields(self):
        identity=service.consumer_process_identity(os.getpid())
        self.assertEqual(service.consumer_process_identity(os.getpid()),identity)
        self.assertEqual(identity['pid'],os.getpid())
        for pid in (True,1,-1,1.5):
            with self.subTest(pid=pid),self.assertRaises(service.ServiceError):service.consumer_process_identity(pid)
        # Linux stat field22 remains correct even if comm contains spaces and ')'.
        fields=['S']+['0']*18+['123456']+['0']*5
        raw=str(os.getpid())+' (name ) with spaces) '+' '.join(fields)
        with mock.patch.object(service.sys,'platform','linux'),\
             mock.patch.object(service.Path,'read_text',return_value='01234567-89ab-cdef-8123-456789abcdef\n'),\
             mock.patch.object(service.Path,'open',return_value=io.StringIO(raw)):
            parsed=service.consumer_process_identity(os.getpid())
        self.assertEqual(parsed,{'kind':'linux-proc-start/v1','pid':os.getpid(),'boot_id':'01234567-89ab-cdef-8123-456789abcdef','start_ticks':123456})
        with mock.patch.object(service.sys,'platform','unsupported'),self.assertRaises(service.ServiceError):service.consumer_process_identity(os.getpid())



if __name__=='__main__':unittest.main(verbosity=2)
