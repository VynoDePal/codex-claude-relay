#!/usr/bin/python3
"""Local stdio MCP connection to the authenticated Claude Code CLI."""
import fcntl,json,os,re,signal,subprocess,sys,time,uuid
from pathlib import Path

CLAUDE=Path.home()/'.local/bin/claude'
STATE=Path.home()/'.local/share/codex-claude-bridge'
TOOLS=[
 {'name':'ask_claude','description':
  'Send a message to Claude Code using the user\'s existing Claude login and return its actual reply. '
  'Use when the user asks to talk to Claude, get Claude\'s opinion, or relay a question to Claude. '
  'Omit session_id for a new conversation; pass the returned session_id for follow-ups. '
  'Only the prompt and this Claude conversation are sent: Codex history and files are not automatically shared. '
  'This conversational connection has no file, shell, web or MCP tools; include any authorized context in prompt. '
  'Do not claim Claude edited files or executed tasks. Distinguish its reply from your own. May take up to 300 seconds.',
  'inputSchema':{'type':'object','properties':{'prompt':{'type':'string','minLength':1,'maxLength':200000},'session_id':{'type':'string','description':'ID returned by a prior ask_claude call or list_conversations.'}},'required':['prompt'],'additionalProperties':False}},
 {'name':'list_conversations','description':'List Claude conversations created by this Codex connection, newest first. Use their session_id to continue an exchange. This does not read unrelated Claude sessions.',
  'inputSchema':{'type':'object','properties':{},'additionalProperties':False}}
]

TOOLS.extend([
 {'name':'start_coding_task','description':
  'Delegate a user-authorized coding task to Claude Code in an explicit project directory. Claude can read and edit project files. '
  'Starts in the background and returns task_id/session_id: use get_coding_task to retrieve its actual result. '
  'Pass session_id from a prior coding task to continue with full Claude context, including feedback and test failures. '
  'Codex must inspect the resulting diff and execute proposed test commands through its own normal execution/approval tools; '
  'Claude has no shell tool in this connection. Relay test failures back to Claude when needed. '
  'Never delegate an action denied by Codex or use this tool to bypass permissions. '
  'Do not edit the same project concurrently. Changes are made in the selected directory; do not assume a worktree. '
  'The Codex conversation is not automatically included: provide requirements and necessary context in prompt.',
  'inputSchema':{'type':'object','properties':{'prompt':{'type':'string','minLength':1,'maxLength':200000},'project_path':{'type':'string','description':'Absolute path of the project authorized by the user.'},'session_id':{'type':'string'}},'required':['prompt','project_path'],'additionalProperties':False}},
 {'name':'get_coding_task','description':'Read the state and final reply of a Claude coding task. Use wait_seconds up to 20 to wait briefly. A completed reply is not proof that tests passed: review changes and run tests through Codex. Permission denials and errors are returned explicitly.',
  'inputSchema':{'type':'object','properties':{'task_id':{'type':'string'},'wait_seconds':{'type':'number','minimum':0,'maximum':20}},'required':['task_id'],'additionalProperties':False}},
 {'name':'cancel_coding_task','description':'Cancel a Claude coding task when the user requests it. Existing edits are preserved; inspect them before continuing.',
  'inputSchema':{'type':'object','properties':{'task_id':{'type':'string'}},'required':['task_id'],'additionalProperties':False}}
])

MODEL_DESCRIPTION=(
 'Optional Claude Code model alias (sonnet, opus, haiku, fable) or exact model ID available to the user. '
 'Codex may choose a model appropriate for the delegated task; always honor an explicit user choice. '
 'Omit to keep the previous choice in this conversation, or use the Claude Code default for a new conversation. '
 'Set a different model to switch on a follow-up while preserving context. Availability is checked by Claude, not assumed.'
)
for tool in TOOLS:
 if tool['name'] in ('ask_claude','start_coding_task'):
  tool['inputSchema']['properties']['model']={'type':'string','minLength':1,'maxLength':128,'description':MODEL_DESCRIPTION}
  tool['description']+=' Choose the Claude model with the optional model parameter. Results report requested_model and models_used from CLI usage, which can include auxiliary model calls.'

def validate_model(model):
 if model is None:return None
 if not isinstance(model,str) or len(model)>128 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*(?:\[1m\])?',model):
  raise ValueError('model must be a Claude model alias or ID (1–128 characters, no whitespace or command options).')
 return model

def invoke(argv,prompt,cwd,timeout=300):
 env=dict(os.environ);env.pop('CLAUDECODE',None)
 p=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=cwd,env=env,start_new_session=True)
 def stop():
  if p.poll() is None:
   os.killpg(p.pid,signal.SIGTERM)
   try:p.communicate(timeout=5)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.communicate()
 try:out,err=p.communicate(prompt,timeout=timeout)
 except subprocess.TimeoutExpired:
  stop()
  raise TimeoutError(f'Claude did not finish within {timeout} seconds. Partial edits or conversation history may exist.')
 except BaseException:
  stop();raise
 if p.returncode:raise RuntimeError(f'Claude Code exited with status {p.returncode}. '+(out or err)[-2000:])
 try:return json.loads(out)
 except ValueError:raise RuntimeError('Claude returned an unexpected response format.') from None

class Bridge:
 def __init__(self,state=STATE):
  self.state=Path(state);self.state.mkdir(mode=0o700,parents=True,exist_ok=True)
  self.index=self.state/'sessions.json'
 def load(self):return json.loads(self.index.read_text()) if self.index.exists() else {}
 def sessions(self):return sorted(self.load().values(),key=lambda x:x['updated_at'],reverse=True)[:50]
 def ask(self,args):
  prompt=args.get('prompt');selected_model=validate_model(args.get('model'))
  if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>200000:raise ValueError('prompt must contain 1–200000 characters.')
  sid=args.get('session_id')
  if sid is not None:
   try:sid=str(uuid.UUID(sid))
   except (ValueError,TypeError,AttributeError):raise ValueError('Invalid session_id.') from None
  with (self.state/'sessions.lock').open('a') as lock:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except BlockingIOError:raise RuntimeError('Another Claude request is running. Wait for its reply before retrying.') from None
   sessions=self.load()
   if sid and sid not in sessions:raise ValueError('Unknown conversation. Use an ID returned by this connection.')
   if sid and sessions[sid].get('mode')=='coding':raise ValueError('Use start_coding_task to continue a coding session.')
   selected_model=selected_model if selected_model is not None else sessions.get(sid,{}).get('requested_model')
   resume=bool(sid);sid=sid or str(uuid.uuid4())
   argv=[str(CLAUDE),'-p','--output-format','json','--safe-mode','--tools','','--strict-mcp-config','--permission-mode','manual','--permission-prompts','none',('--resume' if resume else '--session-id'),sid]
   if selected_model is not None:argv.extend(['--model',selected_model])
   result=invoke(argv,prompt,self.state)
   if result.get('is_error'):raise RuntimeError(str(result.get('result') or result.get('errors') or 'Claude request failed.'))
   if result.get('session_id')!=sid or not isinstance(result.get('result'),str):raise RuntimeError('Claude returned an invalid session or reply.')
   row=sessions.get(sid,{'session_id':sid,'title':prompt.strip().splitlines()[0][:100],'created_at':time.time()})
   row.update(updated_at=time.time(),requested_model=selected_model,last_models_used=list(result.get('modelUsage',{})));sessions[sid]=row
   tmp=self.state/f'.sessions-{uuid.uuid4()}.tmp'
   fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
   with os.fdopen(fd,'w') as f:json.dump(sessions,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
   os.replace(tmp,self.index)
   return {'session_id':sid,'reply':result['result'],'requested_model':selected_model,'models_used':row['last_models_used'],'model':row['last_models_used']}
 def handle(self,method,params):
  if method=='initialize':return {'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'claude-code-connection','version':'1.2.0'}}
  if method=='ping':return {}
  if method=='tools/list':return {'tools':TOOLS}
  if method=='tools/call':
   try:
    name=params.get('name');args=params.get('arguments') or {}
    if name=='ask_claude':result=self.ask(args)
    elif name=='list_conversations':result={'conversations':self.sessions()}
    elif name in ('start_coding_task','get_coding_task','cancel_coding_task'):
     from coding import CodingJobs
     jobs=CodingJobs(self)
     result={'start_coding_task':jobs.start,'get_coding_task':jobs.get,'cancel_coding_task':jobs.cancel}[name](args)
    else:raise ValueError('Unknown tool.')
    return {'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}],'isError':False}
   except Exception as exc:return {'content':[{'type':'text','text':str(exc)}],'isError':True}
  raise LookupError('Method not found')

def main():
 os.umask(0o077);b=Bridge()
 for line in sys.stdin:
  rid=None
  try:
   req=json.loads(line)
   if 'id' not in req:continue
   rid=req['id'];result=b.handle(req.get('method'),req.get('params') or {})
   response={'jsonrpc':'2.0','id':rid,'result':result}
  except Exception as exc:response={'jsonrpc':'2.0','id':rid,'error':{'code':-32601 if isinstance(exc,LookupError) else -32603,'message':str(exc)}}
  print(json.dumps(response,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
