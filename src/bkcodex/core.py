from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib


def now():
    return datetime.now(timezone.utc).isoformat()


def name_checked(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value):
        raise ValueError('Names use 1-80 letters, digits, underscores or hyphens')
    return value


def save(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def load(path):
    return json.loads(Path(path).read_text())


def resolved(value, base):
    p = Path(os.path.expandvars(str(value))).expanduser()
    return (base/p).resolve() if not p.is_absolute() else p.resolve()


@dataclass(frozen=True)
class Config:
    path: Path
    data: dict

    @classmethod
    def read(cls, path):
        path = Path(path).expanduser().resolve()
        data = tomllib.loads(path.read_text())
        name_checked(data['project']['name'])
        cfg = cls(path, data)
        if not cfg.workspace.is_dir(): raise ValueError('Workspace directory does not exist')
        if not 1 <= cfg.workers <= 32: raise ValueError('Worker count must be 1-32')
        if cfg.attempts < 1 or cfg.task_timeout < 1 or cfg.runtime_hours <= 0:
            raise ValueError('Attempts and time limits must be positive')
        if cfg.codex.get('sandbox','workspace-write') not in ('read-only','workspace-write','danger-full-access'):
            raise ValueError('Invalid Codex sandbox')
        if not isinstance(cfg.data.get('validation',{}).get('command',[]), list):
            raise ValueError('Validation command must be an argv array')
        return cfg

    @property
    def name(self): return self.data['project']['name']
    @property
    def workspace(self): return resolved(self.data['project']['workspace'],self.path.parent)
    @property
    def state(self):
        default=f'~/.local/state/bkcodex/{self.name}'
        return resolved(self.data['project'].get('state_dir',default),self.path.parent)
    @property
    def home(self): return resolved(self.codex.get('home',str(self.state/'codex-home')),self.path.parent)
    @property
    def codex(self): return self.data.get('codex',{})
    @property
    def workers(self): return int(self.data.get('workers',{}).get('count',3))
    @property
    def attempts(self): return int(self.data.get('workers',{}).get('max_attempts',2))
    @property
    def task_timeout(self): return float(self.data.get('workers',{}).get('task_timeout_seconds',1800))
    @property
    def runtime_hours(self): return float(self.data.get('workers',{}).get('max_runtime_hours',24))
    @property
    def binary(self):
        value = self.codex.get('binary','codex')
        if '/' in value: return str(resolved(value,self.path.parent))
        return shutil.which(value) or value
    @property
    def roles(self): return [f'worker-{i:02}' for i in range(1,self.workers+1)]

    def init(self):
        for path in (self.state,self.home,self.state/'runs',self.state/'workers'):
            path.mkdir(parents=True,exist_ok=True); path.chmod(0o700)
        # A dedicated login directory starts empty and is authenticated by `login`.
        Queue(self)

    def env(self):
        env={k:v for k,v in os.environ.items() if not k.startswith(('CODEX_','VSCODE_'))}
        env.update(CODEX_HOME=str(self.home),CODEX_SQLITE_HOME=str(self.home),PYTHONUNBUFFERED='1')
        env.update({str(k):str(v) for k,v in self.data.get('environment',{}).items()})
        if env['CODEX_HOME'] != str(self.home) or env['CODEX_SQLITE_HOME'] != str(self.home):
            raise ValueError('Configure Codex home in [codex]')
        return env


class Queue:
    def __init__(self,cfg):
        self.cfg=cfg; cfg.state.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, prompt TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, owner TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL, updated TEXT NOT NULL, run_dir TEXT, error TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, utc TEXT NOT NULL, task TEXT, event TEXT, detail TEXT)''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.cfg.state/'queue.sqlite',timeout=30)
        db.row_factory=sqlite3.Row
        try:
            yield db; db.commit()
        except BaseException:
            db.rollback(); raise
        finally: db.close()

    @staticmethod
    def event(db,task,event,detail=None):
        db.execute('INSERT INTO events(utc,task,event,detail) VALUES(?,?,?,?)',
                   (now(),task,event,json.dumps(detail or {},ensure_ascii=False)))

    def add(self,task_id,prompt,payload=None):
        name_checked(task_id)
        if not prompt.strip(): raise ValueError('Prompt is empty')
        body=json.dumps(payload or {},ensure_ascii=False,sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if old:
                if old['prompt'] != prompt or old['payload'] != body:
                    raise ValueError('Task ID already exists with different content')
                return dict(old)
            db.execute('INSERT INTO tasks(id,prompt,payload,status,created,updated) VALUES(?,?,?,?,?,?)',
                       (task_id,prompt,body,'queued',now(),now()))
            self.event(db,task_id,'queued')
        return {'id':task_id,'status':'queued'}

    def claim(self,owner):
        name_checked(owner)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # A process holds its worker lock before resuming an interrupted task.
            row=db.execute("SELECT * FROM tasks WHERE owner=? AND status='running' ORDER BY created LIMIT 1",(owner,)).fetchone()
            if row is None:
                row=db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if row is None: return None
            if row['attempts'] >= self.cfg.attempts:
                db.execute("UPDATE tasks SET status='blocked',error='attempt limit',updated=? WHERE id=?",(now(),row['id']))
                self.event(db,row['id'],'blocked',{'reason':'attempt limit'}); return {'skip':True}
            db.execute("UPDATE tasks SET owner=?,status='running',attempts=attempts+1,updated=? WHERE id=?",(owner,now(),row['id']))
            self.event(db,row['id'],'claimed',{'owner':owner})
            return dict(db.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone())

    def finish(self,task_id,owner,status,run_dir,error=''):
        if status not in ('completed','blocked','needs_input','queued','interrupted'):
            raise ValueError('Invalid outcome')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cur=db.execute("UPDATE tasks SET status=?,run_dir=?,error=?,updated=? WHERE id=? AND owner=? AND status='running'",
                           (status,str(run_dir),error,now(),task_id,owner))
            if cur.rowcount != 1: raise ValueError('Task owner or state changed')
            self.event(db,task_id,status,{'owner':owner,'run_dir':str(run_dir),'error':error})

    def running_dir(self,task_id,owner,run_dir):
        with self.db() as db:
            cur=db.execute("UPDATE tasks SET run_dir=?,updated=? WHERE id=? AND owner=? AND status='running'",
                (str(run_dir),now(),task_id,owner))
            if cur.rowcount != 1: raise ValueError('Task owner or state changed')

    def retry(self,task_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status FROM tasks WHERE id=?',(task_id,)).fetchone()
            if row is None or row['status'] not in ('blocked','needs_input','interrupted'):
                raise ValueError('Retry requires blocked, needs_input or interrupted task')
            db.execute("UPDATE tasks SET status='queued',owner=NULL,attempts=0,updated=? WHERE id=?",(now(),task_id))
            self.event(db,task_id,'manual_retry')

    def rows(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT id,status,owner,attempts,updated,run_dir,error FROM tasks ORDER BY created,id')]


def cli_args(cfg,run_dir):
    c=cfg.codex
    args=[cfg.binary,*c.get('global_args',[]),'-a','never','exec','--skip-git-repo-check','--json',
          '-m',c.get('model','gpt-6.1-sol'),'-c',f'model_reasoning_effort={json.dumps(c.get("reasoning_effort","high"))}',
          '-c',f'service_tier={json.dumps(c.get("service_tier","priority"))}',
          '-c','features.multi_agent=false','--sandbox',c.get('sandbox','workspace-write'),
          '-C',str(cfg.workspace),'--add-dir',str(cfg.state),'-o',str(run_dir/'final.txt')]
    for directory in c.get('writable_dirs',[]): args += ['--add-dir',str(resolved(directory,cfg.path.parent))]
    return args+['-']


def failure_kind(message):
    text=message.lower()
    if any(v in text for v in ('401','unauthorized','refresh token','refresh_token','sign in','authentication')): return 'auth'
    if any(v in text for v in ('usage limit','insufficient_quota','quota exceeded','credit balance')): return 'quota'
    if any(v in text for v in ('timed out','timeout','stream disconnected','connection','429','502','503','504')): return 'network'
    return 'work'


def validate_result(cfg,run_dir,task):
    path=run_dir/'result.json'
    result=load(path)
    if result.get('task_id') != task['id']: raise ValueError('Result task_id differs')
    if result.get('status') not in ('completed','blocked','needs_input'): raise ValueError('Invalid result status')
    if not isinstance(result.get('summary'),str) or not result['summary'].strip(): raise ValueError('Result summary required')
    artifacts=result.get('artifacts')
    if not isinstance(artifacts,list): raise ValueError('Result artifacts must be a list')
    if result['status']=='completed' and not artifacts: raise ValueError('Completed tasks require artifacts')
    for raw in artifacts:
        artifact=resolved(raw,run_dir)
        if not any(artifact.is_relative_to(root) for root in (cfg.state,cfg.workspace)):
            raise ValueError('Artifact must be within state or workspace')
        if not artifact.is_file(): raise ValueError('Artifact missing: '+str(artifact))
    command=cfg.data.get('validation',{}).get('command',[])
    if command and result['status']=='completed':
        argv=[arg.replace('{task}',str(run_dir/'task.json')).replace('{result}',str(path)).replace('{run_dir}',str(run_dir)) for arg in command]
        with (run_dir/'validation.log').open('w') as log:
            process=subprocess.run(argv,cwd=cfg.workspace,env=cfg.env(),stdout=log,stderr=subprocess.STDOUT,
                timeout=int(cfg.data.get('validation',{}).get('timeout_seconds',120)))
        if process.returncode: raise ValueError('Domain validator failed; see validation.log')
    save(run_dir/'receipt.json',dict(utc=now(),task_id=task['id'],status=result['status'],
        result=str(path),domain_validator_used=bool(command),artifacts=artifacts))
    return result


def task_prompt(task,run_dir):
    # Preserve the submitted task instructions and append only I/O routing.
    return task['prompt']+f'''

任务编号：{task['id']}
任务与载荷：{run_dir}/task.json
本轮输出目录：{run_dir}
已有运行目录：{task.get('run_dir') or '无'}
读取已有工件和检查点，接续未完成部分。实际任务要求以以上正文和载荷为准。
完成当前任务后，将结果写入 {run_dir}/result.json，返回并结束本轮。
结果字段为 task_id、status、summary、artifacts。status 使用 completed、blocked 或 needs_input；artifacts 为实际工件路径列表。
需要续接时保存 CHECKPOINT.json，写明完成范围、下一步和具体缺项。
'''


def execute(cfg,task,owner):
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    run_dir=cfg.state/'runs'/task['id']/stamp; run_dir.mkdir(parents=True)
    task=dict(task,payload=json.loads(task['payload']))
    save(run_dir/'task.json',task)
    Queue(cfg).running_dir(task['id'],owner,run_dir)
    prompt=task_prompt(task,run_dir); (run_dir/'instruction.txt').write_text(prompt)
    progress=dict(worker=owner,pid=os.getpid(),state='running',task_id=task['id'],run_dir=str(run_dir),started=now())
    worker_file=cfg.state/'workers'/(owner+'.json'); save(worker_file,progress)
    result=dict(returncode=None,usage={},error='',run_dir=str(run_dir),thread_id=None)
    deadline=time.monotonic()+cfg.task_timeout; interrupted=False; timed_out=False
    with (run_dir/'events.jsonl').open('w') as events,(run_dir/'stderr.log').open('w') as stderr:
        proc=subprocess.Popen(cli_args(cfg,run_dir),cwd=cfg.workspace,env=cfg.env(),text=False,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,start_new_session=True)
        progress['codex_pid']=proc.pid;save(worker_file,progress)
        proc.stdin.write(prompt.encode()); proc.stdin.close()
        selector=selectors.DefaultSelector(); selector.register(proc.stdout,selectors.EVENT_READ)
        buffer=b''
        try:
            while selector.get_map():
                if time.monotonic()>deadline:
                    timed_out=True; result['error']='task timeout'; break
                for key,_ in selector.select(timeout=1):
                    chunk=os.read(key.fd,65536)
                    if not chunk:
                        selector.unregister(key.fileobj); continue
                    buffer+=chunk
                    while b'\n' in buffer:
                        line,buffer=buffer.split(b'\n',1)
                        text=line.decode(errors='replace');events.write(text+'\n');events.flush()
                        try: event=json.loads(text)
                        except ValueError: continue
                        if event.get('type')=='thread.started': result['thread_id']=event.get('thread_id')
                        if event.get('type')=='turn.completed': result['usage']=event.get('usage',{})
                        if event.get('type') in ('turn.failed','error'):
                            error=event.get('error',{})
                            result['error']=error.get('message','') if isinstance(error,dict) else str(error)
                            result['error']=result['error'] or event.get('message','')
                        item=event.get('item',{})
                        if event.get('type')=='item.completed' and item.get('type')=='error': result['error']=item.get('message','')
                        if event.get('type')=='item.completed' and item.get('type')=='agent_message':
                            print(f"[{owner}] {item.get('text','')}",flush=True)
                        progress.update(last_event=now(),thread_id=result['thread_id']);save(worker_file,progress)
        except (KeyboardInterrupt,SystemExit):
            interrupted=True;result['error']='worker interrupted'
        finally:
            selector.close()
            if proc.poll() is None and (interrupted or timed_out):
                os.killpg(proc.pid,signal.SIGINT)
                try: proc.wait(timeout=10)
                except subprocess.TimeoutExpired: os.killpg(proc.pid,signal.SIGKILL)
            result['returncode']=proc.wait();proc.stdout.close()
            if buffer: events.write(buffer.decode(errors='replace'))
    result.update(interrupted=interrupted,timed_out=timed_out,finished=now())
    save(run_dir/'execution.json',result)
    return run_dir,result


def worker(cfg,owner):
    name_checked(owner);cfg.init();queue=Queue(cfg)
    with (cfg.state/'workers'/(owner+'.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        window=cfg.state/'RUN_WINDOW.json'
        deadline=load(window)['deadline_epoch'] if window.exists() else time.time()+cfg.runtime_hours*3600
        failures=0;exit_reason='queue_empty'
        while time.time() < deadline:
            if (cfg.state/'STOP').exists(): exit_reason='user_paused';break
            cooldown=cfg.state/'NETWORK_COOLDOWN.json'
            while cooldown.exists() and time.time()<load(cooldown).get('until_epoch',0) and time.time()<deadline:
                if (cfg.state/'STOP').exists(): break
                time.sleep(1)
            if (cfg.state/'STOP').exists(): exit_reason='user_paused';break
            task=queue.claim(owner)
            if task is None: break
            if task.get('skip'): continue
            print(f"[{now()}] {owner} claimed {task['id']}",flush=True)
            run_dir=None
            try:
                run_dir,execution=execute(cfg,task,owner)
                if execution['interrupted']:
                    queue.finish(task['id'],owner,'interrupted',run_dir,'worker interrupted')
                    exit_reason='user_interrupted';break
                if execution['returncode'] or execution['timed_out']:
                    raise RuntimeError(execution['error'] or f"Codex exited {execution['returncode']}")
                result=validate_result(cfg,run_dir,task)
                queue.finish(task['id'],owner,result['status'],run_dir)
                failures=0
                print(f"[{owner}] {task['id']}: {result['status']}",flush=True)
            except Exception as exc:
                failures+=1;kind=failure_kind(str(exc))
                retry=kind=='network' and task['attempts']<cfg.attempts
                queue.finish(task['id'],owner,'queued' if retry else 'blocked',run_dir or '',str(exc))
                print(f"[{owner}] {task['id']}: {kind}: {exc}",flush=True)
                if kind in ('auth','quota') or failures>=3:
                    exit_reason=kind;break
                if retry:
                    until=time.monotonic()+float(cfg.data.get('workers',{}).get('retry_delay_seconds',60))
                    save(cfg.state/'NETWORK_COOLDOWN.json',dict(until_epoch=time.time()+float(cfg.data.get('workers',{}).get('retry_delay_seconds',60))))
                    while time.monotonic()<until and not (cfg.state/'STOP').exists(): time.sleep(1)
        else: exit_reason='runtime_limit'
        save(cfg.state/'workers'/(owner+'.json'),dict(worker=owner,state='stopped',reason=exit_reason,updated=now()))


def status(cfg):
    rows=Queue(cfg).rows();counts={}
    for row in rows: counts[row['status']]=counts.get(row['status'],0)+1
    workers={}
    for path in (cfg.state/'workers').glob('*.json'):
        item=load(path);pid=item.get('pid')
        item['process_alive']=bool(pid and Path('/proc',str(pid)).exists());workers[path.stem]=item
    return dict(project=cfg.name,total=len(rows),counts=counts,paused=(cfg.state/'STOP').exists(),
                workers=workers,tasks=rows,state_dir=str(cfg.state))


def unit_name(cfg,role): return f'bkcodex-{cfg.name}-{name_checked(role)}.service'


def unit_quote(value):
    value=str(value)
    if '\n' in value or '\r' in value or '\x00' in value: raise ValueError('Invalid systemd value')
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'


def unit_path(value):
    value=str(value)
    if any(c in value for c in ('\n','\r','\x00')): raise ValueError('Invalid systemd path')
    return value.replace('%','%%')


def unit_text(cfg,role):
    args=[sys.executable,'-m','bkcodex','--config',str(cfg.path),'worker','--name',role]
    source=str(Path(__file__).resolve().parents[1])
    return f'''[Unit]
Description=bkcodex {cfg.name} {role}
ConditionPathExists=!{unit_path(cfg.state)}/STOP
StartLimitIntervalSec=300
StartLimitBurst=3
[Service]
Type=simple
WorkingDirectory={unit_path(cfg.workspace)}
ExecStart={' '.join(unit_quote(arg) for arg in args)}
Environment={unit_quote('PYTHONPATH='+source)}
Environment={unit_quote('PATH='+os.environ.get('PATH','/usr/local/bin:/usr/bin:/bin'))}
Environment=PYTHONUNBUFFERED=1
UMask=0077
Restart=on-failure
RestartSec=30
RuntimeMaxSec={int(cfg.runtime_hours*3600)}
KillMode=control-group
KillSignal=SIGINT
TimeoutStopSec=20
StandardOutput=journal
StandardError=journal
'''


def install_services(cfg):
    cfg.init();directory=Path.home()/'.config/systemd/user';directory.mkdir(parents=True,exist_ok=True)
    for role in cfg.roles: (directory/unit_name(cfg,role)).write_text(unit_text(cfg,role))
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    return {'installed':[unit_name(cfg,r) for r in cfg.roles]}


def start_services(cfg):
    if (cfg.state/'STOP').exists(): raise ValueError('Batch is paused; use resume')
    save(cfg.state/'RUN_WINDOW.json',dict(started=now(),deadline_epoch=time.time()+cfg.runtime_hours*3600))
    subprocess.run(['systemctl','--user','start',*[unit_name(cfg,r) for r in cfg.roles]],check=True)
    sessions=[]
    if shutil.which('tmux'):
        for role in cfg.roles:
            session=f'bkcodex-{cfg.name}-{role}'; sessions.append(session)
            if subprocess.run(['tmux','has-session','-t',session],capture_output=True).returncode:
                subprocess.run(['tmux','new-session','-d','-s',session,
                    'journalctl','--user','-u',unit_name(cfg,role),'-n','30','-f','-o','cat'],check=True)
    return {'started':[unit_name(cfg,r) for r in cfg.roles],'tmux':sessions}


def doctor(cfg):
    linger=subprocess.run(['loginctl','show-user',str(os.getuid()),'-p','Linger','--value'],capture_output=True,text=True) if shutil.which('loginctl') else None
    return dict(python=sys.version.split()[0],workspace_exists=cfg.workspace.is_dir(),
        codex_binary=cfg.binary,codex_exists=Path(cfg.binary).is_file(),
        isolated_home=str(cfg.home),auth_file_exists=(cfg.home/'auth.json').is_file(),
        systemctl=bool(shutil.which('systemctl')),tmux=bool(shutil.which('tmux')),
        linger=linger.stdout.strip() if linger else 'unavailable',
        model=cfg.codex.get('model','gpt-6.1-sol'),reasoning_effort=cfg.codex.get('reasoning_effort','high'),
        service_tier=cfg.codex.get('service_tier','priority'))
