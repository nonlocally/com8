"""Fixed authenticated service endpoints for the existing outbound bus worker.

The only caller identity is the broker poll envelope. Operator configuration is
local; remote messages cannot choose an endpoint, token, executable or user ID.
"""
import concurrent.futures
from contextlib import contextmanager, ExitStack
import ctypes
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
from pathlib import Path
import sqlite3
import ssl
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID

MAX_BODY = 65536
MAX_REPLY = 32768
MAX_PENDING = 128
PROTOCOL = "service-fence/epoch-v1"
_LOCKS = {}
_LOCKS_MUTEX = threading.Lock()
_HELD = threading.local()
_LOCK_PID = os.getpid()
SOURCE_AT_IMPORT = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def contract_version(value):
    if type(value) is not int or value not in (1, 2):
        raise ServiceError("unsupported service contract version")
    return value


class ServiceError(ValueError):
    pass


def endpoint(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ServiceError("service endpoint must be a fixed HTTPS URL")
    return value


def credential(path):
    """Read a regular private owner file without following its final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ServiceError("service credential must be a private owner file")
        if not 32 <= info.st_size <= 4096:
            raise ServiceError("service credential has invalid size")
        token = os.read(fd, 4097).decode("ascii").strip()
        if not 32 <= len(token) <= 4096 or any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126 for ch in token):
            raise ServiceError("invalid service credential")
        return token
    finally:
        os.close(fd)


def registration(args):
    service_id = str(UUID(args.service_id))
    endpoint(args.endpoint)
    token_file = str(Path(args.token_file).absolute())
    credential(token_file)
    return {"kind": "service", "session_key": "service:" + service_id, "name": args.name,
            "service_id": service_id, "endpoint": args.endpoint, "token_file": token_file,
            "contract_version": contract_version(getattr(args, "contract_version", 1)), "status": "queueable"}


def configured(record):
    try:
        endpoint(record["endpoint"])
        credential(record["token_file"])
        UUID(record["service_id"])
        contract_version(record.get("contract_version", 1))
        return True
    except (OSError, ValueError, KeyError, UnicodeError):
        return False


def journal(root):
    path = Path(root) / "service-jobs.sqlite"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ServiceError("invalid service journal")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA synchronous=FULL")
    db.execute("BEGIN IMMEDIATE")
    db.execute("""CREATE TABLE IF NOT EXISTS jobs(
        hub TEXT NOT NULL, id TEXT NOT NULL, recipient TEXT NOT NULL, service_id TEXT NOT NULL,
        envelope TEXT NOT NULL, request_hash TEXT NOT NULL, expires REAL NOT NULL,
        operation TEXT NOT NULL DEFAULT 'deliver', state TEXT NOT NULL DEFAULT 'pending',
        reply TEXT, receipt TEXT, next_at REAL NOT NULL DEFAULT 0, updated REAL NOT NULL,
        PRIMARY KEY(hub,id))""")
    # Existing admitted jobs retain v1 even if their registration is upgraded.
    if "contract_version" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
        db.execute("ALTER TABLE jobs ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 1")
    for name,declaration in [("protocol","TEXT"),("sender_epoch","INTEGER"),("recipient_epoch","INTEGER"),("retirement_barrier","TEXT")]:
        if name not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}:
            db.execute("ALTER TABLE jobs ADD COLUMN "+name+" "+declaration)
    db.execute("""CREATE TABLE IF NOT EXISTS service_lifecycles(
        hub TEXT NOT NULL,recipient TEXT NOT NULL,service_id TEXT NOT NULL,protocol TEXT NOT NULL,
        epoch INTEGER NOT NULL,blocked_through_epoch INTEGER NOT NULL,phase TEXT NOT NULL,
        barrier_id TEXT NOT NULL,request_hash TEXT NOT NULL,request_id TEXT NOT NULL,receipt TEXT,
        PRIMARY KEY(hub,recipient,service_id))""")
    db.execute("CREATE TABLE IF NOT EXISTS service_retirement_barriers(id TEXT PRIMARY KEY,evidence TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS service_job_observations(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,hub TEXT NOT NULL,job_id TEXT NOT NULL,recipient TEXT NOT NULL,
        service_id TEXT NOT NULL,recipient_epoch INTEGER,kind TEXT NOT NULL,result TEXT NOT NULL,updated REAL NOT NULL)""")
    db.commit()
    return db


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _epoch(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ServiceError("invalid service epoch")
    return value


def _actor(record):
    hub, recipient, service_id = record["url"], record["id"], record["service_id"]
    if not isinstance(hub,str) or not isinstance(service_id,str): raise ServiceError("invalid exact service actor")
    parsed = urllib.parse.urlsplit(hub)
    if ((parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1","::1"))) or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/") or hub.endswith("/")):
        raise ServiceError("invalid canonical service hub")
    if (not isinstance(recipient,str) or len(recipient)!=34 or not recipient.startswith("a_")
            or any(ch not in "0123456789abcdef" for ch in recipient[2:])
            or str(UUID(service_id)) != service_id):
        raise ServiceError("invalid exact service actor")
    return hub,recipient,service_id


def service_epoch(record):
    if record.get("fence_protocol") is None:
        return None
    if record.get("fence_protocol") != PROTOCOL:
        raise ServiceError("unsupported service fence protocol")
    return _epoch(record.get("epoch"))


@contextmanager
def lifecycle_lock(root, hub, recipient, service_id, timeout_s=15):
    """Private process-shared and thread-shared actor lock, reentrant per thread."""
    global _LOCK_PID, _LOCKS, _LOCKS_MUTEX, _HELD
    if _LOCK_PID != os.getpid():
        # Do not adopt a parent's reentrant state after fork.
        for value in getattr(_HELD,"actors",{}).values():
            try: os.close(value[1])
            except OSError: pass
        _LOCK_PID=os.getpid(); _LOCKS={}; _LOCKS_MUTEX=threading.Lock(); _HELD=threading.local()
    _actor({"url":hub,"id":recipient,"service_id":service_id})
    if type(timeout_s) not in (int,float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 60:
        raise ServiceError("invalid lifecycle lock timeout")
    root=Path(root)
    if root.is_symlink(): raise ServiceError("unsafe lifecycle root")
    root=root.resolve()
    info=root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o022:
        raise ServiceError("unsafe lifecycle root")
    directory=root/'service-lifecycle-locks'
    try: directory.mkdir(mode=0o700)
    except FileExistsError: pass
    info=directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700):
        raise ServiceError("unsafe lifecycle lock directory")
    key=_hash([str(root),hub,recipient,service_id]); deadline=time.monotonic()+timeout_s
    with _LOCKS_MUTEX:
        if key not in _LOCKS:
            if len(_LOCKS)>=1024: raise ServiceError("lifecycle lock capacity reached")
            _LOCKS[key]=threading.RLock()
        thread_lock=_LOCKS[key]
    if not thread_lock.acquire(timeout=max(0,deadline-time.monotonic())):
        raise ServiceError("service lifecycle is busy")
    held=getattr(_HELD,'actors',None)
    if held is None: held={}; _HELD.actors=held
    fd=None
    try:
        if key in held:
            held[key][0]+=1
        else:
            fd=os.open(directory/(key+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1
                    or stat.S_IMODE(info.st_mode)!=0o600): raise ServiceError("unsafe service lifecycle lock")
            while True:
                try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic()>=deadline: raise ServiceError("service lifecycle is busy")
                    time.sleep(min(.01,max(0,deadline-time.monotonic())))
            held[key]=[1,fd]; fd=None
        yield
    finally:
        if fd is not None: os.close(fd)
        if key in held:
            held[key][0]-=1
            if held[key][0]==0:
                owned=held.pop(key)[1]; fcntl.flock(owned,fcntl.LOCK_UN); os.close(owned)
        thread_lock.release()


def _private_json(path,limit=1024*1024):
    path=Path(path)
    if path.is_symlink(): raise ServiceError("unsafe private proof file")
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1
                or info.st_mode&0o077 or info.st_size>limit): raise ServiceError("unsafe private proof file")
        raw=os.read(fd,limit+1)
        if len(raw)>limit: raise ServiceError("private proof exceeds limit")
        return json.loads(raw),hashlib.sha256(raw).hexdigest()
    finally: os.close(fd)


def consumer_process_identity(pid):
    """Read a kernel start token, never a wall-clock value supplied by a worker.

    Linux stat field 22 is starttime in boot ticks. macOS PROC_PIDTBSDINFO
    supplies the kernel's timeval via the public libproc ABI (proc_info.h).
    Unsupported/unreadable evidence is intentionally not a ps-time fallback.
    """
    if type(pid) is not int or not 1 < pid < 2**31:
        raise ServiceError("invalid consumer process PID")
    if sys.platform.startswith('linux'):
        try:
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            if str(UUID(boot))!=boot: raise ValueError
            raw=Path('/proc')/str(pid)/'stat'
            with raw.open() as stream: fields=stream.read(8193)
            # comm may itself contain spaces and ')'; the last ')' ends it.
            close=fields.rfind(')'); tail=fields[close+1:].split()
            if (len(fields)>8192 or close<0 or fields.split(' ',1)[0]!=str(pid)
                    or len(tail)<20 or not tail[19].isascii() or not tail[19].isdigit()):
                raise ValueError
            ticks=int(tail[19])
            if ticks<=0: raise ValueError
        except (OSError,ValueError,IndexError):
            raise ServiceError("kernel consumer start identity is unavailable") from None
        return {'kind':'linux-proc-start/v1','pid':pid,'boot_id':boot,'start_ticks':ticks}
    if sys.platform=='darwin':
        class ProcBsdInfo(ctypes.Structure):
            _fields_=[(name,ctypes.c_uint32) for name in (
                'flags','status','xstatus','pid','ppid','uid','gid','ruid','rgid','svuid','svgid','reserved')]+[
                ('comm',ctypes.c_char*16),('name',ctypes.c_char*32),
                ('nfiles',ctypes.c_uint32),('pgid',ctypes.c_uint32),('pjobc',ctypes.c_uint32),
                ('tdev',ctypes.c_uint32),('tpgid',ctypes.c_uint32),('nice',ctypes.c_int32),
                ('start_sec',ctypes.c_uint64),('start_usec',ctypes.c_uint64)]
        try:
            lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
            call=lib.proc_pidinfo
            call.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_uint64,ctypes.c_void_p,ctypes.c_int]
            call.restype=ctypes.c_int
            info=ProcBsdInfo(); size=ctypes.sizeof(info)
            if (size!=136 or call(pid,3,0,ctypes.byref(info),size)!=size or info.pid!=pid
                    or info.start_sec<=0 or info.start_usec>=1000000):
                raise ServiceError("kernel consumer start identity is unavailable")
        except (OSError,AttributeError):
            raise ServiceError("kernel consumer start identity is unavailable") from None
        return {'kind':'macos-libproc-start/v1','pid':pid,'start_sec':info.start_sec,'start_usec':info.start_usec}
    raise ServiceError("kernel consumer start identity is unsupported")



def assert_consumer_proof(root):
    """Validate local loaded-consumer evidence; never infer old PID death."""
    root=Path(root).resolve()
    proof,proof_hash=_private_json(root/'service-consumer-proof.json')
    worker,_=_private_json(root/'worker.json')
    required={'schema','root','old_consumer_pid','current_pid','current_started_at','worker_runtime','source_sha256',
              'process_identity','comm_state'}
    if not isinstance(proof,dict) or set(proof)!=required or proof['schema']!='communicate.service-consumer-proof/v1' or proof['root']!=str(root):
        raise ServiceError("invalid loaded consumer proof")
    old,pid=proof['old_consumer_pid'],proof['current_pid']
    if type(old) is not int or old<=1 or type(pid) is not int or pid<=1 or old==pid:
        raise ServiceError("invalid consumer process identity")
    try: os.kill(old,0)
    except ProcessLookupError: pass
    except OSError: raise ServiceError("old consumer absence is uncertain") from None
    else: raise ServiceError("old consumer PID still exists")
    try: os.kill(pid,0)
    except OSError: raise ServiceError("current consumer is unavailable") from None
    actual_identity=consumer_process_identity(pid)
    comm_state=str(root.parent)
    if (proof['comm_state']!=comm_state or worker.get('comm_state')!=comm_state
            or _canonical(proof['process_identity'])!=_canonical(actual_identity)
            or _canonical(worker.get('process_identity'))!=_canonical(actual_identity)):
        raise ServiceError("consumer kernel identity or resolved scope differs")
    current_service=Path(__file__).read_bytes(); current_bus=Path(__file__).with_name('bus.py').read_bytes()
    sources={'lib/bus.py':hashlib.sha256(current_bus).hexdigest(),'lib/bus_service.py':hashlib.sha256(current_service).hexdigest()}
    runtime=hashlib.sha256(current_bus+current_service).hexdigest()
    heartbeat=worker.get('heartbeat_at')
    if (sources['lib/bus_service.py']!=SOURCE_AT_IMPORT or proof['source_sha256']!=sources or proof['worker_runtime']!=runtime
            or worker.get('runtime')!=runtime or worker.get('pid')!=pid or worker.get('started_at')!=proof['current_started_at']
            or worker.get('state')!='running' or type(heartbeat) not in (int,float)
            or not math.isfinite(heartbeat) or not 0<=time.time()-heartbeat<=30):
        raise ServiceError("loaded consumer source/runtime differs")
    observed=subprocess.run(['/bin/ps','-ww','-p',str(pid),'-o','uid=','-o','command='],capture_output=True,text=True,timeout=5)
    parts=observed.stdout.strip().split(None,1)
    argv=shlex.split(parts[1]) if len(parts)==2 else []
    expected=str(Path(__file__).with_name('bus.py').resolve())
    if (observed.returncode!=0 or not parts or parts[0]!=str(os.getuid())
            or (expected,'__worker') not in list(zip(argv,argv[1:]))
            or argv.count('--comm-state')!=1 or ('--comm-state',comm_state) not in list(zip(argv,argv[1:]))):
        raise ServiceError("consumer command identity differs")
    if consumer_process_identity(pid)!=actual_identity:
        raise ServiceError("consumer kernel identity changed during verification")
    return {'proof_sha256':proof_hash,'worker_runtime':runtime,'source_sha256':sources,
            'old_consumer_pid':old,'current_pid':pid,'current_started_at':proof['current_started_at'],
            'process_identity':actual_identity,'comm_state':comm_state}


def _life(db,record):
    return db.execute("SELECT * FROM service_lifecycles WHERE hub=? AND recipient=? AND service_id=?",_actor(record)).fetchone()


def _allowed(db,record,job=None):
    row=_life(db,record); epoch=service_epoch(record)
    if row is None:
        return epoch is None and (job is None or job['recipient_epoch'] is None)
    if row['phase']!='active' or epoch is None or row['epoch']!=epoch or epoch<=row['blocked_through_epoch']:
        return False
    return job is None or (job['recipient_epoch']==epoch and job['protocol']==PROTOCOL)


def lifecycle_status(root,record):
    db=journal(root)
    try:
        row=_life(db,record)
        return dict(row) if row is not None else None
    finally: db.close()


def quiesce(root,record,*,expected_epoch,request_id):
    """Durably block starts before waiting for any in-flight actor lock."""
    if record.get("kind")!="service": raise ServiceError("only known SERVICE actors may retire")
    _epoch(expected_epoch); actor=_actor(record)
    if not isinstance(request_id,str) or not 1<=len(request_id)<=128 or any(not(c.isalnum() or c in '_.:-') for c in request_id):
        raise ServiceError("invalid retirement request ID")
    request={"op":"service_fence_retire","agent":actor[1],"service_id":actor[2],"expected_epoch":expected_epoch,"request_id":request_id}
    identity=_hash([actor,request]); db=journal(root)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE'); row=_life(db,record)
            if row is not None and row['phase'] in ('quiescing','retired'):
                if row['barrier_id']!=identity or row['request_hash']!=_hash(request): raise ServiceError("another lifecycle barrier owns this actor")
            elif row is not None and (row['phase']!='active' or row['epoch']!=expected_epoch):
                raise ServiceError("retirement epoch differs from active lifecycle")
            else:
                db.execute("""INSERT INTO service_lifecycles(hub,recipient,service_id,protocol,epoch,blocked_through_epoch,phase,barrier_id,request_hash,request_id)
                    VALUES(?,?,?,?,?,-1,'quiescing',?,?,?) ON CONFLICT(hub,recipient,service_id) DO UPDATE SET
                    phase='quiescing',barrier_id=excluded.barrier_id,request_hash=excluded.request_hash,request_id=excluded.request_id""",
                    (*actor,PROTOCOL,expected_epoch,identity,_hash(request),request_id))
        return {"barrier_id":identity,"request":request,"request_hash":_hash(request)}
    finally: db.close()


@contextmanager
def retirement_barrier(root,record,*,expected_epoch,request_id,timeout_s=15):
    assert_consumer_proof(root)
    intent=quiesce(root,record,expected_epoch=expected_epoch,request_id=request_id)
    with lifecycle_lock(root,*_actor(record),timeout_s=timeout_s):
        yield intent


def _snapshot_jobs(db,actor):
    return [dict(row) for row in db.execute('SELECT * FROM jobs WHERE hub=? AND recipient=? AND service_id=? ORDER BY id',actor)]


def complete_retirement(root,record,receipt,*,expected_epoch,request_id,timeout_s=15):
    """Persist the caller-authenticated broker receipt and abandonment atomically.

    This proves only local journal disposition. It never proves execution cleanup,
    native/model consumption or authority to release an identity pool lease.
    """
    if record.get("kind")!="service": raise ServiceError("only known SERVICE actors may retire")
    actor=_actor(record); _epoch(expected_epoch)
    consumer=assert_consumer_proof(root)
    request={"op":"service_fence_retire","agent":actor[1],"service_id":actor[2],"expected_epoch":expected_epoch,"request_id":request_id}
    identity=_hash([actor,request]); retired=_epoch(receipt.get('retired_epoch'))
    _epoch(receipt.get('epoch')); _epoch(receipt.get('previous_epoch'))
    if (receipt.get('historical_replay',False) is not False or receipt.get('protocol')!=PROTOCOL or receipt.get('agent')!=actor[1] or receipt.get('service_id')!=actor[2]
            or receipt.get('previous_epoch')!=expected_epoch or receipt.get('epoch')!=retired or retired!=expected_epoch+1
            or receipt.get('retired') is not True or receipt.get('request_id')!=request_id or receipt.get('payload_hash')!=_hash(request)
            or not isinstance(receipt.get('principal'),str) or not re.fullmatch(r'p_[0-9a-f]{32}',receipt['principal'])
            or not isinstance(receipt.get('server_id'),str) or not 1<=len(receipt['server_id'])<=128):
        raise ServiceError("retirement receipt identity mismatch")
    with lifecycle_lock(root,*actor,timeout_s=timeout_s):
        db=journal(root)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE'); row=_life(db,record)
                if row is None or row['barrier_id']!=identity or row['request_hash']!=_hash(request):
                    raise ServiceError("retirement intent missing or changed")
                prior=db.execute('SELECT evidence FROM service_retirement_barriers WHERE id=?',(identity,)).fetchone()
                if prior:
                    evidence=json.loads(prior[0])
                    if evidence['receipt']!=receipt or row['phase']!='retired': raise ServiceError("retirement receipt/lifecycle changed")
                    return evidence
                if row['phase']!='quiescing' or row['epoch']!=expected_epoch:
                    raise ServiceError("retirement lifecycle changed")
                if row['receipt'] is not None and json.loads(row['receipt'])!=receipt:
                    raise ServiceError("retirement receipt changed")
                db.execute("UPDATE service_lifecycles SET receipt=? WHERE hub=? AND recipient=? AND service_id=?",(_canonical(receipt),*actor))
            # Durable authenticated receipt precedes every journal disposition.
            # Crash here resumes this same request/receipt; no remote replay with
            # another key and no assumption that the broker did nothing.
            with db:
                db.execute('BEGIN IMMEDIATE'); row=_life(db,record)
                if row['phase']!='quiescing' or row['epoch']!=expected_epoch or json.loads(row['receipt'])!=receipt:
                    raise ServiceError("retirement lifecycle changed")
                pending=db.execute("SELECT * FROM jobs WHERE hub=? AND recipient=? AND state='pending'",actor[:2]).fetchall()
                if any(job['service_id']!=actor[2] or (job['recipient_epoch'] is not None and job['recipient_epoch']>expected_epoch) for job in pending):
                    raise ServiceError("retirement found another/newer generation")
                before=_hash(_snapshot_jobs(db,actor))
                db.execute("UPDATE jobs SET state='abandoned_retired',retirement_barrier=?,updated=? WHERE hub=? AND recipient=? AND service_id=? AND state='pending'",(identity,time.time(),*actor))
                db.execute("UPDATE jobs SET retirement_barrier=? WHERE hub=? AND recipient=? AND service_id=? AND retirement_barrier IS NULL",(identity,*actor))
                after=_hash(_snapshot_jobs(db,actor))
                evidence={"protocol":PROTOCOL,"barrier_id":identity,"hub":actor[0],"agent":actor[1],"service_id":actor[2],
                    "expected_epoch":expected_epoch,"retired_epoch":retired,"request_id":request_id,"request_hash":_hash(request),
                    "receipt":receipt,"before_journal_sha256":before,"after_journal_sha256":after,"abandoned_jobs":len(pending),
                    "native_journal_drained":False,"consumption_claimed":False,"execution_cleanup_claimed":False,
                    "caller_authenticated_receipt":True,"no_inflight_callback_under_lock":True,"consumer_proof":consumer,
                    "callback_observations_sha256":_hash([dict(r) for r in db.execute("SELECT * FROM service_job_observations WHERE hub=? AND recipient=? AND service_id=? ORDER BY sequence",actor)])}
                evidence['evidence_sha256']=_hash(evidence)
                db.execute('INSERT INTO service_retirement_barriers(id,evidence) VALUES(?,?)',(identity,_canonical(evidence)))
                db.execute("UPDATE service_lifecycles SET phase='retired',epoch=?,blocked_through_epoch=?,receipt=? WHERE hub=? AND recipient=? AND service_id=?",(retired,retired,_canonical(receipt),*actor))
            return evidence
        finally: db.close()


def retirement_evidence(root,record,barrier_id):
    """Read a completed, still-blocked exact barrier; flags alone are not proof."""
    with lifecycle_lock(root,*_actor(record)):
        db=journal(root)
        try:
            row=_life(db,record)
            prior=db.execute('SELECT evidence FROM service_retirement_barriers WHERE id=?',(barrier_id,)).fetchone()
            if row is None or prior is None or row['phase']!='retired' or row['barrier_id']!=barrier_id:
                raise ServiceError("exact completed retirement barrier unavailable")
            evidence=json.loads(prior[0]); expected=evidence.pop('evidence_sha256')
            if (_hash(evidence)!=expected or evidence['hub']!=record['url'] or evidence['agent']!=record['id']
                    or evidence['service_id']!=record['service_id'] or evidence['retired_epoch']!=row['blocked_through_epoch']
                    or evidence['after_journal_sha256']!=_hash(_snapshot_jobs(db,_actor(record)))
                    or evidence['callback_observations_sha256']!=_hash([dict(r) for r in db.execute('SELECT * FROM service_job_observations WHERE hub=? AND recipient=? AND service_id=? ORDER BY sequence',_actor(record))])):
                raise ServiceError("retirement barrier evidence changed")
            evidence['evidence_sha256']=expected
            return evidence
        finally: db.close()


@contextmanager
def guard_completed_retirement_barriers(root,scope,actor_refs,timeout_s=15):
    """Read actual completed records and hold sorted actor locks through caller CAS.

    This does not validate SDK/resource stop or pool/bridge ownership; the caller
    separately proves those dimensions before its own exact release transaction.
    """
    if type(timeout_s) not in (int,float) or not math.isfinite(timeout_s) or not 0<timeout_s<=60:
        raise ServiceError("invalid retirement guard timeout")
    original=Path(root).absolute(); root=original.resolve()
    if original!=root: raise ServiceError("retirement guard root cannot be a symlink alias")
    if (not isinstance(scope,dict) or set(scope)!={'hub','principal','comm_state'}
            or Path(scope['comm_state']).absolute()!=Path(scope['comm_state']).resolve()
            or Path(scope['comm_state']).resolve()/'bus'!=root
            or not isinstance(scope['principal'],str) or not re.fullmatch(r'p_[0-9a-f]{32}',scope['principal'])
            or not isinstance(actor_refs,list) or not 1<=len(actor_refs)<=35):
        raise ServiceError("invalid retirement guard scope")
    expected={'role','service_id','registration_id','slot_fence','claim_sha256','barrier_id','barrier_sha256','previous_epoch','retired_epoch'}
    records=[]; seen=set()
    for ref in actor_refs:
        if (not isinstance(ref,dict) or set(ref)!=expected or not isinstance(ref['role'],str)
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}',ref['role']) or ref['role'] in seen
                or type(ref['slot_fence']) is not int or ref['slot_fence']<1
                or any(not isinstance(ref[k],str) or not re.fullmatch(r'[0-9a-f]{64}',ref[k]) for k in ('claim_sha256','barrier_id','barrier_sha256'))
                or _epoch(ref['retired_epoch'])!=_epoch(ref['previous_epoch'])+1):
            raise ServiceError("invalid exact actor barrier reference")
        record={'kind':'service','url':scope['hub'],'id':ref['registration_id'],'service_id':ref['service_id']}
        actor=_actor(record)
        if actor in seen: raise ServiceError("duplicate service barrier actor")
        seen.add(ref['role']);seen.add(actor);records.append((actor,record,ref))
    deadline=time.monotonic()+timeout_s
    with ExitStack() as stack:
        for actor,record,ref in sorted(records,key=lambda value:value[0]):
            remaining=deadline-time.monotonic()
            if remaining<=0: raise ServiceError("retirement guard timed out")
            stack.enter_context(lifecycle_lock(root,*actor,timeout_s=min(60,remaining)))
        consumer=assert_consumer_proof(root); actors=[]
        for actor,record,ref in records:
            evidence=retirement_evidence(root,record,ref['barrier_id'])
            if (evidence['evidence_sha256']!=ref['barrier_sha256'] or evidence['expected_epoch']!=ref['previous_epoch']
                    or evidence['retired_epoch']!=ref['retired_epoch'] or evidence['receipt']['principal']!=scope['principal']
                    or evidence['consumer_proof']!=consumer):
                raise ServiceError("completed retirement reference changed")
            actors.append({**ref,'phase':'retired','blocked_through_epoch':evidence['retired_epoch'],
                'broker_receipt_sha256':_hash(evidence['receipt']),'journal_before_sha256':evidence['before_journal_sha256'],
                'journal_after_sha256':evidence['after_journal_sha256'],'consumer_runtime_sha256':consumer['worker_runtime'],
                'consumer_proof_sha256':consumer['proof_sha256'],'callbacks_inflight':0})
        yield {'schema':'communicate.service-retirement-barriers-snapshot/v1','protocol':PROTOCOL,
               'scope':dict(scope),'actors':actors,'consumer_proof':consumer}


def activate_generation(root,record):
    if record.get("kind")!="service": raise ServiceError("only known SERVICE actors may activate")
    actor=_actor(record); epoch=service_epoch(record)
    if epoch is None: raise ServiceError("fenced registration metadata required")
    with lifecycle_lock(root,*actor):
        db=journal(root)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE'); row=_life(db,record)
                if row is None: raise ServiceError("missing known lifecycle barrier")
                if row['phase']=='active' and row['epoch']==epoch: return dict(row)
                if row['phase']!='retired' or epoch!=row['blocked_through_epoch']+1:
                    raise ServiceError("registration differs from completed retirement")
                db.execute("UPDATE service_lifecycles SET phase='active',epoch=? WHERE hub=? AND recipient=? AND service_id=?",(epoch,*actor))
                return dict(_life(db,record))
        finally: db.close()


def normalized(envelope, version=1):
    # Discard the delivery lease and all presentation labels. These assertions
    # come only from broker poll; never parse identity fields from message text.
    keys = ("id", "target", "bus", "reply_to", "message", "created_at", "expires_at", "conversation_expires_at")
    result = {key: envelope[key] for key in keys}
    result["sender"] = {key: envelope["sender"][key] for key in ("id", "kind", "user", "device_id")}
    if contract_version(version) == 2:
        # A legacy queued reply has no stored parent. Never mistake missing
        # correlation for a new request and trigger another agent turn.
        if type(envelope.get("correlation_version")) is not int or envelope["correlation_version"] != 1:
            raise ServiceError("broker reply correlation is unavailable")
        conversation = envelope.get("conversation_id")
        if not isinstance(conversation, str) or not re.fullmatch(r"(?:c|hc)_[0-9a-f]{32}", conversation):
            raise ServiceError("invalid broker conversation identity")
        if "in_reply_to" not in envelope:
            raise ServiceError("broker reply correlation is unavailable")
        parent = envelope["in_reply_to"]
        if parent is not None and (not isinstance(parent, str) or not re.fullmatch(r"(?:m|hm)_[0-9a-f]{32}", parent)):
            raise ServiceError("invalid broker reply identity")
        result.update(correlation_version=1, conversation_id=conversation, in_reply_to=parent)
    if envelope.get("protocol") is not None:
        if envelope.get("protocol") != PROTOCOL: raise ServiceError("unsupported service envelope protocol")
        result["protocol"]=PROTOCOL
        recipient_epoch=envelope.get("recipient_epoch")
        result["recipient_epoch"]=None if recipient_epoch is None else _epoch(recipient_epoch)
        sender_epoch=envelope["sender"].get("epoch")
        result["sender"]["epoch"]=None if sender_epoch is None else _epoch(sender_epoch)
    return result



def enqueue(root,record,envelope):
    with lifecycle_lock(root,*_actor(record)):
        return _enqueue_locked(root,record,envelope)


def _enqueue_locked(root, record, envelope):
    if envelope["target"] != record["id"] or envelope["reply_to"] != envelope["id"]:
        raise ServiceError("service delivery identity mismatch")
    expiry = envelope["conversation_expires_at"]
    if (not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
            or not math.isfinite(expiry) or expiry <= time.time() or expiry > time.time() + 86401):
        raise ServiceError("invalid service reply deadline")
    db = journal(root)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM jobs WHERE hub=? AND id=?", (record["url"], envelope["id"])).fetchone()
            version = contract_version(old["contract_version"] if old else record.get("contract_version", 1))
            item = normalized(envelope, version)
            body = json.dumps(item, sort_keys=True, separators=(",", ":"))
            if len(body.encode()) > MAX_BODY - 2048:
                raise ServiceError("service message exceeds limit")
            digest = hashlib.sha256(body.encode()).hexdigest()
            if not _allowed(db,record,{"recipient_epoch":item.get("recipient_epoch"),"protocol":item.get("protocol")}):
                raise ServiceError("service lifecycle is blocked or generation differs")
            if old:
                if old["request_hash"] != digest or old["service_id"] != record["service_id"]:
                    raise ServiceError("service message identity conflict")
            else:
                count = db.execute("SELECT COUNT(*) FROM jobs WHERE state='pending'").fetchone()[0]
                if count >= MAX_PENDING:
                    raise ServiceError("service transport queue is full")
                db.execute("""INSERT INTO jobs(hub,id,recipient,service_id,envelope,request_hash,expires,updated,contract_version,protocol,sender_epoch,recipient_epoch)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (record["url"], item["id"], record["id"],
                              record["service_id"], body, digest, expiry, time.time(), version,item.get("protocol"),item["sender"].get("epoch"),item.get("recipient_epoch")))
        return "queued", "durably queued for service; task acceptance and answer not yet confirmed"
    finally:
        db.close()



class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def bridge(record, job):
    payload = {"version": contract_version(job["contract_version"]), "op": job["operation"], "hub": job["hub"],
               "recipient_id": job["recipient"], "service_id": job["service_id"],
               "envelope": json.loads(job["envelope"])}
    body = json.dumps(payload).encode()
    if len(body) > MAX_BODY:
        raise ServiceError("service request exceeds limit")
    req = urllib.request.Request(endpoint(record["endpoint"]), data=body, method="POST", headers={
        "Authorization": "Bearer " + credential(record["token_file"]), "Content-Type": "application/json"})
    context = ssl.create_default_context()
    # Match the broker client: Apple's Python can ignore SSL_CERT_FILE in
    # load_default_certs. Explicitly honor operator-installed CAs, never skip TLS.
    if os.environ.get("SSL_CERT_FILE"):
        context.load_verify_locations(cafile=os.environ["SSL_CERT_FILE"])
    handlers = [NoRedirect(), urllib.request.HTTPSHandler(context=context)]
    if urllib.parse.urlsplit(record["endpoint"]).hostname in ("localhost", "127.0.0.1", "::1"):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=15) as response:
        if response.status != 200:
            raise ServiceError("service endpoint returned invalid status")
        raw = response.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise ServiceError("service response exceeds limit")
    return validate_response(json.loads(raw), job["contract_version"])


def validate_response(result, version):
    contract_version(version)
    if not isinstance(result, dict) or type(result.get("pending")) is not bool:
        raise ServiceError("invalid service response")
    if result["pending"]:
        if set(result) != {"pending"}:
            raise ServiceError("invalid pending service response")
    elif version == 2 and result == {"pending": False, "disposition": "acknowledge"}:
        pass
    elif (set(result) != {"pending", "reply"} or not isinstance(result["reply"], str)
          or not result["reply"].strip() or "\x00" in result["reply"]
          or not 1 <= len(result["reply"].encode()) <= MAX_REPLY):
        raise ServiceError("invalid terminal service response")
    return result


def _advance_job(root,record,selected,connection,broker_request,call_bridge,now):
    errors=[]
    with lifecycle_lock(root,*_actor(record)):
        db=journal(root)
        try:
            row=db.execute("SELECT * FROM jobs WHERE hub=? AND id=? AND state='pending'",(selected['hub'],selected['id'])).fetchone()
            if row is None or not _allowed(db,record,row): return errors
            job=dict(row)
            # CAS always binds both nullable generation fields and pending state.
            where="hub=? AND id=? AND state='pending' AND recipient_epoch IS ? AND protocol IS ?"
            identity=(job['hub'],job['id'],job['recipient_epoch'],job['protocol'])
            def update(sql,values):
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    if not _allowed(db,record,job): return False
                    return db.execute("UPDATE jobs SET "+sql+" WHERE "+where,(*values,*identity)).rowcount==1
            def observation(kind,result):
                with db:
                    db.execute("INSERT INTO service_job_observations(hub,job_id,recipient,service_id,recipient_epoch,kind,result,updated) VALUES(?,?,?,?,?,?,?,?)",
                        (job['hub'],job['id'],job['recipient'],job['service_id'],job['recipient_epoch'],kind,_canonical(result),now))
            if job['reply'] is None:
                try:
                    result=validate_response(call_bridge(record,job),job['contract_version'])
                    acknowledged=result.get('disposition')=='acknowledge'
                    reply=result.get('reply')
                except Exception:
                    update('next_at=?,updated=?',(now+5,now))
                    return [{"agent":job['recipient'],"error":"service endpoint unavailable; retry scheduled"}]
                if acknowledged:
                    if not update("state='acknowledged',updated=?",(now,)): observation("callback_after_quiesce",result)
                    return errors
                if not update("operation='poll',reply=?,next_at=?,updated=?",(reply,now+5,now)):
                    observation("callback_after_quiesce",result)
                    return errors
                job['reply']=reply
            if job['reply'] is not None:
                # Quiesce can be published while a callback holds the actor lock.
                # Recheck immediately before beginning another remote effect.
                if not _allowed(db,record,job): return errors
                try:
                    args={"sender":job['recipient'],"id":json.loads(job['envelope'])['reply_to'],
                          "message":job['reply'],"request_id":"service:"+hashlib.sha256((job['hub']+'|'+job['id']).encode()).hexdigest()}
                    if job['recipient_epoch'] is not None: args['expected_sender_epoch']=job['recipient_epoch']
                    receipt=broker_request(connection,'reply',**args)
                    if not update("state='sent',receipt=?,updated=?",(_canonical(receipt),now)):
                        observation("broker_reply_after_quiesce",receipt)
                except Exception as exc:
                    state='expired' if getattr(exc,'code',None) in {'not_found','forbidden','unauthorized'} else 'pending'
                    update('state=?,next_at=?,updated=?',(state,now+5,now))
                    errors.append({"agent":job['recipient'],"error":"service reply unavailable; "+state})
            return errors
        finally: db.close()


def advance(root, registrations, connections, broker_request, *, call_bridge=bridge, now=None):
    """Bounded drain; actor lock spans callbacks/replies, never a SQLite transaction."""
    now=time.time() if now is None else now
    db=journal(root)
    try:
        with db:
            db.execute("UPDATE jobs SET state='expired',updated=? WHERE state='pending' AND expires<=?",(now,now))
            # Retirement evidence is append-only, including the preserved job row.
            db.execute("DELETE FROM jobs WHERE state NOT IN ('pending','abandoned_retired') AND retirement_barrier IS NULL AND updated<?",(now-172800,))
        records={(r['url'],r['id']):r for r in registrations.values() if r.get('kind')=='service'}
        jobs=db.execute("SELECT * FROM jobs WHERE state='pending' AND next_at<=? ORDER BY next_at,updated LIMIT 128",(now,)).fetchall()
        available=[]
        for job in jobs:
            record=records.get((job['hub'],job['recipient']))
            if (record and record['service_id']==job['service_id'] and job['hub'] in connections
                    and (record.get('buses') or record.get('reply_until',0)>now) and _allowed(db,record,job)):
                available.append((dict(job),dict(record)))
        available=available[:8]
    finally: db.close()
    errors=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures=[pool.submit(_advance_job,root,record,job,connections[job['hub']],broker_request,call_bridge,now)
                 for job,record in available]
        for future in concurrent.futures.as_completed(futures):
            try: errors.extend(future.result())
            except Exception: errors.append({"error":"service lifecycle unavailable; retained for retry"})
    return errors
