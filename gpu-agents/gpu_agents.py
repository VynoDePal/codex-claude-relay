"""Private, persistent GPU coding workers using the installed Codex client."""
import argparse
import contextlib
import copy
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

BINARY = '/usr/lib/chatgpt/resources/codex'
ROOT = Path.home()/'.local/share/codex-gpu-agents'
BASE_URL = 'http://127.0.0.1:4012/v1'
MODELS = {'glm':'glm-5.3-flash', 'deepseek':'deepseek-v4.1',
          'glm-5.3-flash':'glm-5.3-flash', 'deepseek-v4.1':'deepseek-v4.1'}
DEFAULTS = {'max_agents':10, 'concurrency':2, 'timeout_seconds':900,
            'enabled_models':['glm-5.3-flash']}
ACTIVE = {'starting','running','cancelling'}
ROLES = {'implementer','tester','reviewer'}
EFFORTS = {'low','high','xhigh','max','ultra'}
BOOT_ID = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
INSTRUCTIONS = '''You are a GPU coding worker delegated by the user through Codex Desktop.
Work only on the assigned task in the explicit project directory. Read the project's
AGENTS.md instructions. Preserve unrelated edits. Do not delegate to other agents,
change Codex settings, access credential files, send messages, publish, deploy, push,
or perform unrelated actions. Never bypass a denied permission or alter sandboxing.
If a permission or provider error blocks the task, report it and stop.
Run relevant tests if permitted. Your final reply must identify changed files,
commands actually executed with their outcomes, and remaining failures or limitations.
Do not claim tests passed unless they actually ran successfully.
'''


def atomic(path, value):
    path = Path(path)
    tmp = path.with_name('.'+path.name+'-'+uuid.uuid4().hex)
    fd = os.open(tmp, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(value,out,ensure_ascii=False,indent=2);out.flush();os.fsync(out.fileno())
        os.replace(tmp,path)
    finally:
        if tmp.exists():tmp.unlink()


def validate_settings(value):
    if not isinstance(value,dict) or set(value)!=set(DEFAULTS):
        raise ValueError('Settings must contain exactly max_agents, concurrency, timeout_seconds and enabled_models.')
    for key,lo,hi in [('max_agents',1,32),('concurrency',1,4),('timeout_seconds',30,1800)]:
        number=value[key]
        if type(number) is not int or not lo<=number<=hi:raise ValueError(f'{key} must be {lo}–{hi}.')
    models=value['enabled_models']
    if not isinstance(models,list) or not models or len(set(models))!=len(models) or any(m not in set(MODELS.values()) for m in models):
        raise ValueError('enabled_models must list supported, unique GPU model IDs.')
    return copy.deepcopy(value)


def task_prompt(value):
    if not isinstance(value,str) or not value.strip() or len(value)>120000:
        raise ValueError('prompt must contain 1–120000 characters.')
    return value.strip()


def process_identity(pid):
    try:
        fields=Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')',1)[1].split()
        if fields[0]=='Z':return None
        return {'pid':int(pid),'start_ticks':fields[19],'boot_id':BOOT_ID}
    except (ValueError,OSError,IndexError,TypeError):return None


def alive(identity):
    return bool(identity and process_identity(identity.get('pid'))==identity)


def descendants(identity):
    """Snapshot identities before parents can exit and descendants get reparented."""
    if not alive(identity):return []
    processes={}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():continue
        try:
            fields=(path/'stat').read_text().rsplit(')',1)[1].split()
            if fields[0]=='Z':continue
            processes[int(path.name)]=(int(fields[1]),{'pid':int(path.name),'start_ticks':fields[19],'boot_id':BOOT_ID})
        except (OSError,ValueError,IndexError):continue
    if processes.get(identity['pid'],(None,None))[1]!=identity:return []
    owned={identity['pid']:identity}
    while True:
        extra={pid:record for pid,(parent,record) in processes.items() if parent in owned and pid not in owned}
        if not extra:break
        owned.update(extra)
    return list(owned.values())


def signal_owned(identity,signum):
    """Bind the signal to a PID descriptor, then recheck identity to prevent reuse."""
    try:fd=os.pidfd_open(identity['pid'])
    except ProcessLookupError:return
    try:
        if alive(identity):signal.pidfd_send_signal(fd,signum)
    except ProcessLookupError:pass
    finally:os.close(fd)


def stop_process(identity,known=()):
    targets={t['pid']:t for t in [*known,*descendants(identity)] if alive(t)}
    for target in reversed(list(targets.values())):signal_owned(target,signal.SIGTERM)
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        remaining=[t for t in targets.values() if alive(t)]
        if not remaining:return
        # Track children created during shutdown while their parent is still ours.
        for parent in remaining:
            for target in descendants(parent):
                if target['pid'] not in targets:
                    targets[target['pid']]=target;signal_owned(target,signal.SIGTERM)
        time.sleep(0.05)
    for target in reversed(list(targets.values())):signal_owned(target,signal.SIGKILL)
    deadline=time.monotonic()+2
    while any(alive(t) for t in targets.values()) and time.monotonic()<deadline:time.sleep(0.02)
    if any(alive(t) for t in targets.values()):raise RuntimeError('Worker descendants have not stopped; task capacity remains reserved.')


class Store:
    def __init__(self,root=ROOT):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.root.chmod(0o700)
        for name in ('teams','tasks','logs'):
            (self.root/name).mkdir(exist_ok=True,mode=0o700)
        with self.lock():
            if not (self.root/'config.json').exists():atomic(self.root/'config.json',DEFAULTS)

    @contextlib.contextmanager
    def lock(self):
        fd=os.open(self.root/'state.lock',os.O_CREAT|os.O_RDWR,0o600)
        with os.fdopen(fd,'a') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            yield

    def settings(self):return validate_settings(json.loads((self.root/'config.json').read_text()))

    def configure(self,value):
        value=validate_settings(value)
        with self.lock():atomic(self.root/'config.json',value)
        return value

    def path(self,kind,identifier):
        prefix={'teams':'team','tasks':'task'}[kind]
        if not isinstance(identifier,str) or not re.fullmatch(prefix+r'_[a-f0-9]{32}',identifier):
            raise ValueError(f'Invalid {prefix}_id.')
        return self.root/kind/(identifier+'.json')

    def load(self,kind,identifier):
        try:return json.loads(self.path(kind,identifier).read_text())
        except FileNotFoundError:raise ValueError(f'Unknown {kind[:-1]} ID.') from None

    def save(self,row):atomic(self.path('tasks',row['task_id']),row)

    def project(self,value):
        if not isinstance(value,str) or not Path(value).is_absolute():raise ValueError('project_path must be absolute.')
        path=Path(value).resolve()
        protected=[(Path.home()/'.codex').resolve(),self.root]
        if not path.is_dir() or path in (Path('/'),Path.home()) or any(path==p or p in path.parents or path in p.parents for p in protected):
            raise ValueError('Choose an existing, explicit project directory outside Codex private state.')
        return str(path)

    def create_team(self,args):
        project=self.project(args.get('project_path'))
        with self.lock():
            settings=self.settings();maximum=args.get('max_agents',settings['max_agents'])
            if type(maximum) is not int or not 1<=maximum<=settings['max_agents']:
                raise ValueError(f'max_agents must be 1–{settings["max_agents"]}.')
            selected=args.get('allowed_models',settings['enabled_models'])
            if not isinstance(selected,list) or not selected or any(m not in settings['enabled_models'] for m in selected):
                raise ValueError('allowed_models must be a nonempty subset of enabled models.')
            row={'team_id':'team_'+uuid.uuid4().hex,'project_path':project,'max_agents':maximum,
                 'allowed_models':sorted(set(selected)),'agents_created':0,'created_at':time.time()}
            atomic(self.path('teams',row['team_id']),row)
            return row

    def refresh(self,row):
        if row['status'] not in ACTIVE:return row
        if row['status']=='cancelling':
            if time.time()-row['updated_at']>5:
                # Recover an interrupted cancellation without releasing its project early.
                stop_process(row.get('child'),row.get('termination_targets',[]));stop_process(row.get('worker'))
                row.update(status='cancelled',updated_at=time.time());self.save(row)
            return row
        orphan=(row.get('worker') and not alive(row['worker'])) or (not row.get('worker') and time.time()-row['updated_at']>30)
        if orphan:
            stop_process(row.get('child'))
            row.update(status='interrupted',error='Worker stopped before returning. Inspect existing edits before continuing.',updated_at=time.time())
            self.save(row)
        return row

    def check_capacity(self,team,model,role,exclude=None):
        settings=self.settings()
        if model not in settings['enabled_models'] or model not in team['allowed_models']:
            raise ValueError(f'Model {model} is disabled for this team. No automatic fallback is used.')
        current=[]
        for path in (self.root/'tasks').glob('task_*.json'):
            row=self.refresh(json.loads(path.read_text()))
            if row['status'] in ACTIVE and row['task_id']!=exclude:current.append(row)
        if len(current)>=settings['concurrency']:raise ValueError('Global concurrency limit reached. Wait for an existing agent.')
        project=Path(team['project_path'])
        for row in current:
            other=Path(row['project_path'])
            overlap=project==other or project in other.parents or other in project.parents
            if overlap and (role!='reviewer' or row['role']!='reviewer'):
                raise ValueError('Active task directories overlap; wait or use a separate worktree.')
        return settings

    def reserve_task(self,args):
        prompt=task_prompt(args.get('prompt'));role=args.get('role','implementer')
        if role not in ROLES:raise ValueError('Unknown role. Use implementer, tester or reviewer.')
        model=MODELS.get(args.get('model','glm-5.3-flash'))
        if model is None:raise ValueError('Unsupported GPU model.')
        effort=args.get('reasoning_effort','high')
        if effort not in EFFORTS:raise ValueError('Unsupported reasoning_effort.')
        with self.lock():
            team=self.load('teams',args.get('team_id'));self.project(team['project_path'])
            if team['agents_created']>=team['max_agents']:raise ValueError('Team agent limit reached; continue an existing task instead.')
            settings=self.check_capacity(team,model,role)
            row={'task_id':'task_'+uuid.uuid4().hex,'team_id':team['team_id'],'project_path':team['project_path'],
                 'model':model,'role':role,'reasoning_effort':effort,'original_prompt':prompt,'prompt':prompt,
                 'turn_id':'turn_'+uuid.uuid4().hex,'turn_number':1,'status':'starting','created_at':time.time(),
                 'updated_at':time.time(),'timeout_seconds':settings['timeout_seconds'],'agent_number':team['agents_created']+1}
            self.save(row);team['agents_created']+=1;atomic(self.path('teams',team['team_id']),team)
            return row

    def reserve_continue(self,args):
        feedback=task_prompt(args.get('prompt'))
        with self.lock():
            row=self.refresh(self.load('tasks',args.get('task_id')))
            if row['status'] in ACTIVE:raise ValueError('This agent is already running.')
            team=self.load('teams',row['team_id']);self.project(team['project_path'])
            settings=self.check_capacity(team,row['model'],row['role'],row['task_id'])
            previous=row.get('reply','')[-20000:]
            history=row.get('feedback_history',[])+[feedback]
            combined='\n\n'.join(history)
            if len(combined)>60000:raise ValueError('Continuation feedback is too large; create a concise new task.')
            prompt=('Original task:\n'+row['original_prompt']+'\n\nPrevious worker report (verify against files):\n'+previous+
                    '\n\nManager follow-up instructions, oldest to newest:\n'+combined)
            for key in ('worker','child','reply','commands','error','usage','exit_code','finished_at','logs','termination_targets'):
                row.pop(key,None)
            row.update(prompt=prompt,feedback_history=history,turn_id='turn_'+uuid.uuid4().hex,
                       turn_number=row['turn_number']+1,status='starting',updated_at=time.time(),timeout_seconds=settings['timeout_seconds'])
            self.save(row);return row

    def finish(self,identifier,turn_id,result):
        with self.lock():
            row=self.load('tasks',identifier)
            if row['turn_id']!=turn_id or row['status'] not in {'starting','running'}:return
            row.update(result,updated_at=time.time(),finished_at=time.time());self.save(row)

    def launch(self,row):
        try:
            with self.lock():
                current=self.load('tasks',row['task_id'])
                if current['turn_id']!=row['turn_id'] or current['status']!='starting':raise ValueError('Task is no longer pending.')
                log=self.root/'logs'/(row['turn_id']+'-worker.log')
                fd=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as stream:
                    proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--state',str(self.root),
                                           'worker',row['task_id'],row['turn_id']],stdin=subprocess.DEVNULL,
                                          stdout=stream,stderr=stream,start_new_session=True,close_fds=True)
                current.update(worker=process_identity(proc.pid),status='running',updated_at=time.time());self.save(current)
        except Exception:
            self.finish(row['task_id'],row['turn_id'],{'status':'failed','error':'Failed to launch local worker.'})
            raise
        return self.public(current)

    def start(self,args):return self.launch(self.reserve_task(args))
    def resume(self,args):return self.launch(self.reserve_continue(args))

    def read_task(self,identifier):
        with self.lock():return self.refresh(self.load('tasks',identifier))

    def public(self,row):
        return {k:v for k,v in row.items() if k not in ('prompt','original_prompt','feedback_history','worker','child','termination_targets')}

    def get(self,args):
        wait=args.get('wait_seconds',0)
        if type(wait) not in (int,float) or not 0<=wait<=20:raise ValueError('wait_seconds must be 0–20.')
        deadline=time.monotonic()+wait
        while True:
            row=self.read_task(args.get('task_id'))
            if row['status'] not in ACTIVE or time.monotonic()>=deadline:return self.public(row)
            time.sleep(min(0.25,max(0,deadline-time.monotonic())))

    def list_tasks(self,args=None):
        args=args or {};team_id=args.get('team_id')
        if team_id:self.load('teams',team_id)
        with self.lock():
            rows=[self.refresh(json.loads(p.read_text())) for p in (self.root/'tasks').glob('task_*.json')]
        rows=sorted((r for r in rows if not team_id or r['team_id']==team_id),key=lambda r:r['updated_at'],reverse=True)
        keys=('task_id','team_id','project_path','model','role','status','turn_number','updated_at')
        return {'tasks':[{k:r[k] for k in keys} for r in rows[:100]],'settings':self.settings()}

    def cancel(self,args):
        with self.lock():
            row=self.load('tasks',args.get('task_id'))
            if row['status'] not in ACTIVE:return self.public(row)
            if row['status']=='cancelling':return self.public(self.refresh(row))
            targets=descendants(row.get('child'))+descendants(row.get('worker'))
            row.update(status='cancelling',termination_targets=targets,error='Cancellation in progress. Existing file changes are preserved.',updated_at=time.time());self.save(row)
        stop_process(row.get('child'),targets);stop_process(row.get('worker'))
        with self.lock():
            current=self.load('tasks',row['task_id'])
            if current['turn_id']==row['turn_id'] and current['status']=='cancelling':
                current.update(status='cancelled',error='Cancelled. Existing file changes are preserved.',updated_at=time.time());self.save(current)
        return self.public(current)


def build_command(task,output):
    instructions=INSTRUCTIONS
    if task['role']=='reviewer':instructions+='Review only. Do not edit files. Return actionable findings with file references.\n'
    if task['role']=='tester':instructions+='Focus on independent tests. Do not weaken tests to hide a failing implementation.\n'
    return [BINARY,'-a','never','exec','--ignore-user-config','--ephemeral','--skip-git-repo-check',
            '-m',task['model'],'-c','model_provider="openai"','-c','openai_base_url='+json.dumps(BASE_URL),
            '-c','model_reasoning_effort='+json.dumps(task['reasoning_effort']),'-c','service_tier="default"',
            '-c','agents.enabled=false','-c','features.apps=false','-c','features.plugins=false',
            '-c','developer_instructions='+json.dumps(instructions),
            '-s','read-only' if task['role']=='reviewer' else 'workspace-write',
            '--json','-o',str(output),'-']


def parse_events(events,exit_code,reply):
    commands=[];errors=[];complete=False;usage={}
    for event in events:
        kind=event.get('type');item=event.get('item',{})
        if kind=='turn.completed':complete=True;usage=event.get('usage',{})
        if kind in ('turn.failed','error'):
            error=event.get('error',event.get('message','Codex turn failed'))
            errors.append(error.get('message',str(error)) if isinstance(error,dict) else str(error))
        if kind=='item.completed' and item.get('type')=='command_execution':
            commands.append({'command':item.get('command',''),'exit_code':item.get('exit_code'),
                             'output':item.get('aggregated_output','')[-6000:]})
    good=exit_code==0 and complete and not errors and bool(reply.strip())
    result={'status':'completed' if good else 'failed','reply':reply[-40000:],'exit_code':exit_code,
            'commands':commands[-20:],'usage':usage}
    if not good:result['error']='\n'.join(errors)[-6000:] or 'Codex did not return a successful completed turn.'
    return result


def worker(store,task_id,turn_id):
    os.umask(0o077)
    proc=None;identity=None
    try:
        with store.lock():
            task=store.load('tasks',task_id)
            if task['turn_id']!=turn_id or task['status'] not in {'starting','running'}:return
            store.project(task['project_path'])
            if task['model'] not in store.settings()['enabled_models']:raise ValueError('Selected model is disabled.')
            output=store.root/'logs'/(turn_id+'-reply.txt')
            event_path=store.root/'logs'/(turn_id+'-events.jsonl')
            stderr_path=store.root/'logs'/(turn_id+'-stderr.log')
            env={k:v for k,v in os.environ.items() if not (k.startswith(('CODEX_','OPENAI_','ANTHROPIC_','CLAUDE')) or k.endswith(('_API_KEY','_TOKEN')))}
            with event_path.open('wb') as events,stderr_path.open('wb') as errors:
                proc=subprocess.Popen(build_command(task,output),cwd=task['project_path'],env=env,stdin=subprocess.PIPE,
                                      stdout=events,stderr=errors,start_new_session=True,close_fds=True)
            identity=process_identity(proc.pid)
            task.update(child=identity,updated_at=time.time());store.save(task)
        try:
            proc.communicate(task['prompt'].encode(),timeout=task['timeout_seconds'])
            timed_out=False
        except subprocess.TimeoutExpired:
            stop_process(identity);proc.wait(timeout=5);timed_out=True
        rows=[]
        with event_path.open() as stream:
            for line in stream:
                try:rows.append(json.loads(line))
                except ValueError:pass
        reply=output.read_text() if output.exists() else ''
        result=parse_events(rows,proc.returncode,reply)
        if timed_out:result.update(status='timed_out',error=f'Worker exceeded {task["timeout_seconds"]} seconds. Inspect partial edits before continuing.')
        result['logs']={'events':str(event_path),'stderr':str(stderr_path)}
        store.finish(task_id,turn_id,result)
    except Exception as exc:
        if identity:stop_process(identity)
        store.finish(task_id,turn_id,{'status':'failed','error':str(exc)[:2000]})
    finally:
        if proc and proc.poll() is None:stop_process(identity);proc.wait(timeout=5)


def cli():
    parser=argparse.ArgumentParser(description='Configure and inspect GPU coding workers for Codex Desktop.')
    parser.add_argument('--state',type=Path,default=ROOT)
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('status')
    config=sub.add_parser('config');config.add_argument('--max-agents',type=int);config.add_argument('--concurrency',type=int)
    config.add_argument('--timeout-seconds',type=int);config.add_argument('--enable-model',choices=sorted(MODELS))
    config.add_argument('--disable-model',choices=sorted(MODELS))
    w=sub.add_parser('worker');w.add_argument('task_id');w.add_argument('turn_id')
    args=parser.parse_args();store=Store(args.state)
    if args.action=='worker':worker(store,args.task_id,args.turn_id);return
    if args.action=='status':result=store.list_tasks()
    else:
        settings=store.settings()
        for key in ('max_agents','concurrency','timeout_seconds'):
            if getattr(args,key) is not None:settings[key]=getattr(args,key)
        if args.enable_model:settings['enabled_models']=sorted(set(settings['enabled_models']+[MODELS[args.enable_model]]))
        if args.disable_model:settings['enabled_models']=[m for m in settings['enabled_models'] if m!=MODELS[args.disable_model]]
        result=store.configure(settings)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':cli()
