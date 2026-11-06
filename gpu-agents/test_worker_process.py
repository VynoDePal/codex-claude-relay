import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import gpu_agents
from gpu_agents import Store,worker,process_identity,alive,stop_process


class WorkerProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.project=self.root/'project';self.project.mkdir()
        self.store=Store(self.root/'state')
        self.team=self.store.create_team({'project_path':str(self.project)})
        self.fake=self.root/'fake-codex'

    def program(self,body):
        self.fake.write_text('#!'+sys.executable+'\nimport sys,os,json,time,subprocess\nfrom pathlib import Path\n'+body)
        self.fake.chmod(0o700)

    def task(self,timeout=None):
        row=self.store.reserve_task({'team_id':self.team['team_id'],'prompt':'Synthetic worker test'})
        if timeout:
            row['timeout_seconds']=timeout
            with self.store.lock():self.store.save(row)
        return row

    def test_real_subprocess_result_and_private_logs(self):
        self.program('''prompt=sys.stdin.read()
Path('received.txt').write_text(prompt)
Path(sys.argv[sys.argv.index('-o')+1]).write_text('Changed fixture; one real fake-client command recorded.')
print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'synthetic command','exit_code':0,'aggregated_output':'OK'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':7,'output_tokens':4}}))
''')
        task=self.task()
        with patch.object(gpu_agents,'BINARY',str(self.fake)):
            worker(self.store,task['task_id'],task['turn_id'])
        row=self.store.read_task(task['task_id'])
        self.assertEqual(row['status'],'completed',row)
        self.assertEqual(row['commands'][0]['exit_code'],0)
        self.assertEqual((self.project/'received.txt').read_text(),'Synthetic worker test')
        for p in (self.store.root/'logs').iterdir():self.assertEqual(p.stat().st_mode & 0o777,0o600)

    def sleeper(self):
        self.program('''p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
Path('grandchild.pid').write_text(str(p.pid))
time.sleep(60)
''')

    def test_timeout_terminates_command_descendants_and_records_partial_failure(self):
        self.sleeper();task=self.task(timeout=0.4)
        with patch.object(gpu_agents,'BINARY',str(self.fake)):worker(self.store,task['task_id'],task['turn_id'])
        row=self.store.read_task(task['task_id'])
        self.assertEqual(row['status'],'timed_out')
        self.assertFalse(alive(row['child']))
        self.assertIsNone(process_identity(int((self.project/'grandchild.pid').read_text())))

    def test_cancel_stops_descendants_and_cannot_be_overwritten_as_success(self):
        self.sleeper();task=self.task()
        with patch.object(gpu_agents,'BINARY',str(self.fake)):
            thread=threading.Thread(target=worker,args=(self.store,task['task_id'],task['turn_id']))
            thread.start()
            deadline=time.monotonic()+5
            while not (self.project/'grandchild.pid').exists() and time.monotonic()<deadline:time.sleep(0.02)
            self.assertTrue((self.project/'grandchild.pid').exists())
            result=self.store.cancel({'task_id':task['task_id']});thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(self.store.read_task(task['task_id'])['status'],'cancelled')
        self.assertIsNone(process_identity(int((self.project/'grandchild.pid').read_text())))

    def test_pid_reuse_does_not_signal_an_unrelated_process(self):
        p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
        try:
            actual=process_identity(p.pid)
            stop_process({**actual,'start_ticks':'not-the-same-process'})
            self.assertTrue(alive(actual))
        finally:stop_process(process_identity(p.pid));p.wait()

    def test_descendant_ignoring_sigterm_is_killed_after_leader_exits(self):
        child_code="import signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path('ready').touch();time.sleep(60)"
        parent_code=f"import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c',{child_code!r}]);Path('stubborn.pid').write_text(str(p.pid));time.sleep(60)"
        p=subprocess.Popen([sys.executable,'-c',parent_code],cwd=self.project,start_new_session=True)
        child=None
        try:
            deadline=time.monotonic()+5
            while not (self.project/'ready').exists() and time.monotonic()<deadline:time.sleep(0.02)
            child=int((self.project/'stubborn.pid').read_text())
            stop_process(process_identity(p.pid));p.wait(timeout=5)
            self.assertIsNone(process_identity(child),'Descendant ignored SIGTERM and survived the leader.')
        finally:
            if child and process_identity(child):os.kill(child,signal.SIGKILL)
            if p.poll() is None:p.kill();p.wait()


if __name__=='__main__':unittest.main()
