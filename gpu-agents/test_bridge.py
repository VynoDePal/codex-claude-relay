import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from gpu_agents import Store
from bridge import Bridge


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.project=self.root/'project';self.project.mkdir()
        self.bridge=Bridge(Store(self.root/'state'))

    def call(self,name,args):
        result=self.bridge.handle('tools/call',{'name':name,'arguments':args})
        return result,json.loads(result['content'][0]['text']) if not result['isError'] else result['content'][0]['text']

    def test_catalog_exposes_bounded_delegation_and_continuations(self):
        tools={t['name']:t for t in self.bridge.handle('tools/list',{})['tools']}
        self.assertEqual(set(tools),{'create_team','start_task','continue_task','get_task','list_tasks','cancel_task'})
        self.assertEqual(tools['start_task']['inputSchema']['properties']['model']['enum'],['glm-5.3-flash','deepseek-v4.1'])
        self.assertIn('read-only',tools['start_task']['description'])

    def test_unknown_keys_cannot_mutate_settings_or_routes(self):
        result,_=self.call('create_team',{'project_path':str(self.project),'base_url':'https://example.invalid'})
        self.assertTrue(result['isError'])
        result,_=self.call('config',{'enabled_models':['deepseek-v4.1']})
        self.assertTrue(result['isError'])
        self.assertEqual(self.bridge.store.settings()['enabled_models'],['glm-5.3-flash'])

    def test_team_and_tasks_survive_server_restart(self):
        result,team=self.call('create_team',{'project_path':str(self.project),'max_agents':1})
        self.assertFalse(result['isError'])
        row=self.bridge.store.reserve_task({'team_id':team['team_id'],'prompt':'Read','role':'reviewer'})
        self.bridge.store.finish(row['task_id'],row['turn_id'],{'status':'completed','reply':'Read'})
        self.bridge=Bridge(Store(self.root/'state'))
        result,task=self.call('get_task',{'task_id':row['task_id']})
        self.assertFalse(result['isError']);self.assertEqual(task['reply'],'Read')
        self.assertNotIn('prompt',task)

    def test_stdio_initialize_ping_and_errors_are_valid_jsonrpc(self):
        requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
                  {'jsonrpc':'2.0','method':'notifications/initialized'},
                  {'jsonrpc':'2.0','id':2,'method':'ping'},
                  {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_task','arguments':{'task_id':'../secret'}}}]
        p=subprocess.run([sys.executable,str(Path(__file__).with_name('bridge.py')),'--state',str(self.root/'state')],
                         input='\n'.join(json.dumps(x) for x in requests)+'\n',text=True,capture_output=True,timeout=10)
        self.assertEqual(p.returncode,0,p.stderr)
        responses=[json.loads(x) for x in p.stdout.splitlines()]
        self.assertEqual([r['id'] for r in responses],[1,2,3])
        self.assertEqual(responses[0]['result']['serverInfo']['name'],'gpu-agents')
        self.assertTrue(responses[2]['result']['isError'])


if __name__=='__main__':unittest.main()
