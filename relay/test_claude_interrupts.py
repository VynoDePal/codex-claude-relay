import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import claude_interrupts
from claude_interrupts import InterruptWatcher, Proc, stop_thread

CHILD = '30000000-0000-4000-8000-000000000001'
OTHER = '30000000-0000-4000-8000-000000000002'
OPENAI = '30000000-0000-4000-8000-000000000003'
MAIN = '30000000-0000-4000-8000-000000000004'


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')


def gone(pid):
    st = Proc().stat(pid)
    return st is None or st[0] == 'Z'


class InterruptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.day = self.root / 'sessions/2026/11/10'
        self.day.mkdir(parents=True)
        self.watcher = InterruptWatcher(self.root, {'claude-code-opus'})
        self.watcher.scan()

    def spawn(self, tid, *cmd):
        p = subprocess.Popen(list(cmd) or ['sleep', '60'], env={**os.environ, 'CODEX_THREAD_ID': tid}, stdout=subprocess.PIPE)
        def cleanup():
            if p.poll() is None:
                p.kill()
            p.wait()
            p.stdout.close()
        self.addCleanup(cleanup)
        return p

    def append(self, path, *records):
        with path.open('a') as f:
            for r in records:
                f.write(json.dumps(r) + '\n')

    def rollout(self, tid, model='claude-code-opus', source='subagent'):
        path = self.day / ('rollout-test-' + tid + '.jsonl')
        self.append(path, {'type': 'session_meta', 'timestamp': iso(time.time()), 'payload': {'id': tid, 'thread_source': source}},
                    {'type': 'turn_context', 'timestamp': iso(time.time()), 'payload': {'model': model}})
        return path

    def abort(self, path, at):
        self.append(path, {'type': 'event_msg', 'timestamp': iso(at), 'payload': {'type': 'turn_aborted', 'reason': 'interrupted'}})

    def wait_gone(self, pid, seconds=5):
        end = time.time() + seconds
        while time.time() < end:
            if gone(pid):
                return True
            time.sleep(0.05)
        return False

    def test_interrupt_stops_commands_started_before(self):
        path = self.rollout(CHILD)
        p = self.spawn(CHILD)
        time.sleep(0.1)
        self.abort(path, time.time() + 1)
        self.assertEqual(self.watcher.scan(), [(CHILD, [p.pid])])
        self.assertEqual(p.wait(timeout=5), -signal.SIGTERM)

    def test_descendant_without_thread_variable_is_stopped(self):
        path = self.rollout(CHILD)
        p = self.spawn(CHILD, 'bash', '-c', 'env -u CODEX_THREAD_ID sleep 61 & echo $!; wait')
        child = int(p.stdout.readline())
        self.abort(path, time.time() + 1)
        stopped = self.watcher.scan()
        self.assertIn(child, stopped[0][1])
        self.assertTrue(self.wait_gone(child))
        p.wait(timeout=5)

    def test_later_commands_other_threads_and_other_agents_survive(self):
        procs = [self.spawn(t) for t in (CHILD, OTHER, OPENAI, MAIN)]
        time.sleep(0.1)
        self.abort(self.rollout(CHILD), time.time() - 30)
        self.abort(self.rollout(OPENAI, model='gpt-6.1-sol'), time.time() + 1)
        self.abort(self.rollout(MAIN, source='user'), time.time() + 1)
        self.assertEqual(self.watcher.scan(), [(CHILD, [])])
        time.sleep(0.3)
        self.assertEqual([p.poll() for p in procs], [None] * 4)

    def test_history_present_at_startup_is_not_replayed(self):
        path = self.rollout(CHILD)
        p = self.spawn(CHILD)
        self.abort(path, time.time() + 1)
        fresh = InterruptWatcher(self.root, {'claude-code-opus'})
        self.assertEqual(fresh.scan(), [])
        self.assertIsNone(p.poll())

    def test_thread_index_supplies_missing_context(self):
        db = sqlite3.connect(self.root / 'state_5.sqlite')
        db.execute('create table threads (id text, thread_source text, model text)')
        db.execute('insert into threads values (?,?,?)', (CHILD, 'subagent', 'claude-code-opus'))
        db.commit()
        db.close()
        path = self.day / ('rollout-test-' + CHILD + '.jsonl')
        self.append(path, {'type': 'session_meta', 'timestamp': iso(time.time()), 'payload': {'id': CHILD}})
        p = self.spawn(CHILD)
        self.abort(path, time.time() + 1)
        self.assertEqual(self.watcher.scan(), [(CHILD, [p.pid])])
        p.wait(timeout=5)

    def test_codex_processes_are_protected(self):
        class Fake:
            table = {10: ('S', 1, 100.0, CHILD, True), 11: ('S', 1, 100.0, CHILD, False),
                     12: ('S', 11, 101.0, None, False), 13: ('S', 1, 300.0, CHILD, False)}
            def pids(self): return list(self.table)
            def stat(self, pid):
                row = self.table.get(pid)
                return row[:3] if row else None
            def thread(self, pid): return self.table[pid][3]
            def protected(self, pid): return self.table[pid][4]
        fake, calls = Fake(), []
        def kill(pid, sig):
            calls.append((pid, sig))
            fake.table.pop(pid, None)
        self.assertEqual(stop_thread(CHILD, 200.0, fake, kill=kill), [11, 12])
        self.assertEqual(sorted(calls), [(11, signal.SIGTERM), (12, signal.SIGTERM)])

    def test_can_be_disabled(self):
        with mock.patch.dict(os.environ, {'CODEX_INTERRUPT_STOPS_COMMANDS': '0'}):
            self.assertFalse(claude_interrupts.enabled())
        with mock.patch.dict(os.environ, {}):
            os.environ.pop('CODEX_INTERRUPT_STOPS_COMMANDS', None)
            self.assertTrue(claude_interrupts.enabled())


if __name__ == '__main__':
    unittest.main()

