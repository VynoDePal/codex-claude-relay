"""Persistent coding jobs. Claude edits; Codex runs commands under its own policy."""
import contextlib,fcntl,json,os,signal,subprocess,sys,time,uuid
from pathlib import Path

TERMINAL={'completed','needs_permission','failed','timed_out','cancelled','interrupted'}
INSTRUCTIONS='''You are Claude Code working on a coding task delegated by the user through Codex Desktop. Read applicable AGENTS.md and CLAUDE.md instructions in the selected project. Preserve unrelated user changes. Work only on the requested task in this project. You have file read/edit tools; shell execution is handled by Codex with its normal permissions. Do not simulate execution or claim tests passed. When edits are ready, report changed files, a concise explanation, and exact test commands for Codex to review and execute. Codex may resume this same session with test output so you can fix failures. Do not deploy, push, or perform unrelated actions. A permission denial must be reported, never worked around through another tool or agent.'''

def coding_command(sid,resume,model=None):
 from bridge import CLAUDE,validate_model
 model=validate_model(model)
 return [str(CLAUDE),'-p','--output-format','json','--safe-mode','--restricted','--strict-mcp-config','--tools','Read,Glob,Grep,Edit,Write','--permission-mode','acceptEdits','--permission-prompts','none','--append-system-prompt',INSTRUCTIONS,'--resume' if resume else '--session-id',sid]+(['--model',model] if model is not None else [])

def atomic(path,data):
 tmp=path.with_name('.'+path.name+'-'+uuid.uuid4().hex)
 fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
 os.replace(tmp,path)

@contextlib.contextmanager
def locked(path,nonblocking=False):
 with path.open('a') as f:
  try:fcntl.flock(f,fcntl.LOCK_EX|(fcntl.LOCK_NB if nonblocking else 0))
  except BlockingIOError:raise ValueError('Another request is updating this connection. Retry after its reply.') from None
  yield

def birth(pid):
 try:return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
 except (OSError,IndexError):return None

class CodingJobs:
 def __init__(self,bridge):
  self.b=bridge;self.folder=bridge.state/'jobs';self.folder.mkdir(mode=0o700,exist_ok=True)
 def jobpath(self,tid):
  try:tid=str(uuid.UUID(tid))
  except (ValueError,TypeError,AttributeError):raise ValueError('Invalid task_id.') from None
  p=self.folder/(tid+'.json')
  if not p.exists():raise ValueError('Unknown task_id.')
  return p
 def start(self,args):
  from bridge import validate_model
  selected_model=validate_model(args.get('model'))
  prompt=args.get('prompt');project=args.get('project_path')
  if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>200000:raise ValueError('prompt must contain 1–200000 characters.')
  if not isinstance(project,str) or not Path(project).is_absolute():raise ValueError('project_path must be an explicit absolute directory.')
  project=Path(project).resolve()
  if not project.is_dir() or project in (Path('/'),Path('/home'),Path.home()):raise ValueError('Select a specific existing project directory.')
  for protected in (self.b.state,Path.home()/'.codex',Path.home()/'.claude'):
   if project.is_relative_to(protected.resolve()):raise ValueError('Select a project, not the connection or authentication directory.')
  sid=args.get('session_id')
  if sid:
   try:sid=str(uuid.UUID(sid))
   except (ValueError,TypeError,AttributeError):raise ValueError('Invalid session_id.') from None
  with locked(self.b.state/'sessions.lock',True):
   sessions=self.b.load();row=sessions.get(sid) if sid else None
   if sid and (not row or row.get('mode')!='coding'):raise ValueError('Unknown coding session. Omit session_id for a new task.')
   if row and row['project_path']!=str(project):raise ValueError('A coding session cannot switch projects.')
   if row and row.get('active_task') and self.get({'task_id':row['active_task']})['status'] not in TERMINAL:raise ValueError('This coding session already has a running task.')
   # Avoid two Claude jobs editing the same project concurrently.
   for other in sessions.values():
    if other.get('project_path')==str(project) and other.get('active_task') and self.get({'task_id':other['active_task']})['status'] not in TERMINAL:raise ValueError('A Claude coding task is already running in this project.')
   sid=sid or str(uuid.uuid4());tid=str(uuid.uuid4());now=time.time()
   row=row or {'session_id':sid,'mode':'coding','project_path':str(project),'title':prompt.strip().splitlines()[0][:100],'created_at':now,'initialized':False}
   selected_model=selected_model if selected_model is not None else row.get('requested_model')
   job={'requested_model':selected_model,'task_id':tid,'session_id':sid,'project_path':str(project),'prompt':prompt,'resume':row['initialized'],'status':'queued','created_at':now}
   atomic(self.folder/(tid+'.json'),job)
   row.update(active_task=tid,updated_at=now,requested_model=selected_model);sessions[sid]=row;atomic(self.b.index,sessions)
   try:
    with (self.folder/(tid+'.log')).open('a') as log:
     subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',str(self.b.state),tid],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,close_fds=True)
   except Exception:
    job.update(status='failed',error='Could not start Claude worker.');atomic(self.folder/(tid+'.json'),job);raise
  return {k:job[k] for k in ('task_id','session_id','project_path','status','requested_model')}
 def get(self,args):
  p=self.jobpath(args.get('task_id'));wait=args.get('wait_seconds',0)
  if not isinstance(wait,(int,float)) or not 0<=wait<=20:raise ValueError('wait_seconds must be between 0 and 20.')
  end=time.monotonic()+wait
  while True:
   with locked(p.with_suffix('.lock')):
    j=json.loads(p.read_text())
    if j['status']=='running' and birth(j['pid'])!=j['birth']:
     j.update(status='interrupted',error='The worker stopped before returning a result. Edits may already exist.');atomic(p,j)
    if j['status']=='queued' and time.time()-j['created_at']>60:
     j.update(status='failed',error='Worker did not start.');atomic(p,j)
   if j['status'] in TERMINAL or time.monotonic()>=end:break
   time.sleep(min(.2,max(0,end-time.monotonic())))
  out={k:v for k,v in j.items() if k not in ('prompt','birth','pid','resume')}
  out['elapsed_seconds']=round(j.get('finished_at',time.time())-j['created_at'],1)
  return out
 def cancel(self,args):
  p=self.jobpath(args.get('task_id'))
  with locked(p.with_suffix('.lock')):
   j=json.loads(p.read_text())
   if j['status']=='queued':j.update(status='cancelled',finished_at=time.time());atomic(p,j)
   elif j['status']=='running' and birth(j['pid'])==j['birth']:
    os.kill(j['pid'],signal.SIGTERM)
  return self.get({'task_id':args['task_id'],'wait_seconds':1})

class Cancelled(Exception):pass

def worker(state,tid):
 from bridge import Bridge,invoke
 jobs=CodingJobs(Bridge(Path(state)));p=jobs.jobpath(tid)
 def stop(*_):raise Cancelled('Cancelled by the user. Edits already made are preserved.')
 signal.signal(signal.SIGTERM,stop)
 with locked(p.with_suffix('.lock')):
  j=json.loads(p.read_text())
  if j['status']=='cancelled':return
  j.update(status='running',pid=os.getpid(),birth=birth(os.getpid()));atomic(p,j)
 result={}
 try:
  result=invoke(coding_command(j['session_id'],j['resume'],j.get('requested_model')),j['prompt'],j['project_path'],timeout=1800)
  if result.get('session_id')!=j['session_id']:raise RuntimeError('Claude returned an unexpected session_id.')
  denials=result.get('permission_denials',[])
  j.update(status='failed' if result.get('is_error') else ('needs_permission' if denials else 'completed'),reply=result.get('result',''),permission_denials=denials,model=list(result.get('modelUsage',{})),models_used=list(result.get('modelUsage',{})),tests_executed_by_claude=False,next_step='Codex should inspect the changes and run the proposed tests using its normal execution permissions, then resume this session with any failures.')
 except Cancelled as e:j.update(status='cancelled',error=str(e))
 except TimeoutError as e:j.update(status='timed_out',error=str(e))
 except Exception as e:j.update(status='failed',error=str(e))
 finally:
  # Avoid a second cancellation interrupting the final write.
  signal.signal(signal.SIGTERM,signal.SIG_IGN)
  j['finished_at']=time.time()
  with locked(jobs.b.state/'sessions.lock'):
   sessions=jobs.b.load();row=sessions[j['session_id']]
   if result.get('session_id')==j['session_id']:row['initialized']=True
   row['last_models_used']=list(result.get('modelUsage',{}))
   row['updated_at']=time.time();atomic(jobs.b.index,sessions)
  with locked(p.with_suffix('.lock')):atomic(p,j)

if __name__=='__main__':
 os.umask(0o077)
 if len(sys.argv)==4 and sys.argv[1]=='--worker':worker(sys.argv[2],sys.argv[3])
 else:raise SystemExit('Use the MCP bridge to start coding tasks.')
