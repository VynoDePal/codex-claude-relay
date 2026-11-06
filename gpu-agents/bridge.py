#!/usr/bin/python3
"""Small stdio MCP interface for project-bound GPU coding agents."""
import argparse
import json
import os
import sys
from pathlib import Path
from gpu_agents import ROOT,Store


def tool(name,description,properties,required=()):
    return {'name':name,'description':description,'inputSchema':{'type':'object','properties':properties,
            'required':list(required),'additionalProperties':False}}


TASK_ID={'type':'string','description':'task_id returned by this GPU connection.'}
TEAM_ID={'type':'string','description':'team_id returned by create_team.'}
PROMPT={'type':'string','minLength':1,'maxLength':120000,'description':'Self-contained task with plan, relevant files, ownership, acceptance criteria and test commands. Codex history is not automatically shared.'}
TOOLS=[
    tool('create_team',
         'Create a GPU coding team for a user-authorized project. The main Codex model remains the manager. '
         'Use this connection, not native collaboration.spawn_agent, for GPU delegation: native OpenAI agent messages '
         'may contain encrypted task payloads the GPU models cannot read. Returns allowed models and the cumulative agent limit N. '
         'Only select an explicitly authorized project. Do not delegate a permission-denied action. '
         'DeepSeek is disabled by default until the user re-enables it locally.',
         {'project_path':{'type':'string','description':'Absolute project path authorized by the user.'},
          'max_agents':{'type':'integer','minimum':1,'maximum':32,'description':'Requested N, at most the configured global maximum. Omit to use it.'},
          'allowed_models':{'type':'array','minItems':1,'uniqueItems':True,'items':{'type':'string','enum':['glm-5.3-flash','deepseek-v4.1']}}},['project_path']),
    tool('start_task',
         'Delegate implementation, tests or review to a selected GPU model. Starts a real Codex worker in the team project '
         'and returns immediately; use get_task to await the outcome. The manager chooses model and effort. '
         'Default GLM high. reviewer uses read-only; implementer/tester use workspace-write with no privilege escalation '
         'and no interactive approvals. Workers cannot call external connectors or spawn agents. '
         'Never bypass permissions or delegate writes if the parent task lacks write authorization. '
         'No simultaneous writes in overlapping projects; use isolated worktrees/teams for parallel coding. '
         'The manager must inspect the diff and verify actual test commands before accepting the result. '
         'A failed or unavailable model is reported, never silently replaced.',
         {'team_id':TEAM_ID,'prompt':PROMPT,'model':{'type':'string','enum':['glm-5.3-flash','deepseek-v4.1']},
          'role':{'type':'string','enum':['implementer','tester','reviewer']},
          'reasoning_effort':{'type':'string','enum':['low','high','xhigh','max','ultra']}},['team_id','prompt']),
    tool('continue_task',
         'Give a finished GPU worker feedback or test failures. Reuses its model, role, project and slot in N. '
         'Starts a fresh turn with the original task, manager follow-ups, last report and current files; '
         'full model history is not retained. Wait for any running turn first. Preserve parent permissions.',
         {'task_id':TASK_ID,'prompt':PROMPT},['task_id','prompt']),
    tool('get_task',
         'Read a GPU worker state, final report and actual command outcomes. Optional wait up to 20 seconds. '
         'completed means the model turn finished, not that tests necessarily passed. Check commands and inspect files. '
         'Failed, cancelled and timed-out tasks may have partial edits.',
         {'task_id':TASK_ID,'wait_seconds':{'type':'number','minimum':0,'maximum':20}},['task_id']),
    tool('list_tasks','List recent GPU tasks and configured limits. Filter by team_id; use get_task for the report.',{'team_id':TEAM_ID}),
    tool('cancel_task','Stop a GPU task when requested or when the delegated work is no longer needed. Existing edits remain; inspect them before continuing.',{'task_id':TASK_ID},['task_id']),
]


class Bridge:
    def __init__(self,store=None):self.store=store or Store()
    def handle(self,method,params):
        if method=='initialize':
            return {'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'gpu-agents','version':'1.0.0'}}
        if method=='ping':return {}
        if method=='tools/list':return {'tools':TOOLS}
        if method=='tools/call':
            try:
                name=params.get('name');args=params.get('arguments',{})
                definition=next((t for t in TOOLS if t['name']==name),None)
                if definition is None:raise ValueError('Unknown GPU tool.')
                schema=definition['inputSchema']
                if not isinstance(args,dict) or set(args)-set(schema['properties']):raise ValueError('Unknown or invalid tool arguments.')
                if set(schema['required'])-set(args):raise ValueError('Required tool arguments are missing.')
                action={'create_team':self.store.create_team,'start_task':self.store.start,'continue_task':self.store.resume,
                        'get_task':self.store.get,'list_tasks':self.store.list_tasks,'cancel_task':self.store.cancel}[name]
                return {'content':[{'type':'text','text':json.dumps(action(args),ensure_ascii=False)}],'isError':False}
            except Exception as exc:return {'content':[{'type':'text','text':str(exc)}],'isError':True}
        raise LookupError('Method not found')


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('--state',type=Path,default=ROOT)
    bridge=Bridge(Store(parser.parse_args().state))
    for line in sys.stdin:
        rid=None
        try:
            request=json.loads(line)
            if 'id' not in request:continue
            rid=request['id'];result=bridge.handle(request.get('method'),request.get('params') or {})
            response={'jsonrpc':'2.0','id':rid,'result':result}
        except Exception as exc:response={'jsonrpc':'2.0','id':rid,'error':{'code':-32601 if isinstance(exc,LookupError) else -32603,'message':str(exc)}}
        print(json.dumps(response,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
