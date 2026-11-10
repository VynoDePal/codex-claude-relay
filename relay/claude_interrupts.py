"""Stop the shell commands of an interrupted Claude sub-agent.

interrupt_agent ends the sub-agent's turn, but Codex leaves the commands that
turn started running (a sleep 300 survived in tests, with Claude and OpenAI
children alike). Codex writes a turn_aborted event (reason "interrupted") in the
sub-agent's rollout, and every command it runs inherits CODEX_THREAD_ID. The
watcher follows new rollout lines and, for a sub-agent served by this relay,
stops the processes of that thread started before the interruption, with their
descendants. Commands started afterwards (a follow-up task) are left alone.
Set CODEX_INTERRUPT_STOPS_COMMANDS=0 to disable it.
"""
import asyncio
import contextlib
import json
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

GRACE_SECONDS = 2.0
INTERVAL_SECONDS = 2.0
MAX_CHUNK = 64 * 1024 * 1024
# Codex's own processes may carry the variable; never stop them.
PROTECTED = (b'app-server', b'codex-code-mode-host')
_TICK = os.sysconf('SC_CLK_TCK')


def enabled():
    return os.environ.get('CODEX_INTERRUPT_STOPS_COMMANDS', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def epoch(stamp):
    try:
        return datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


class Proc:
    """Read-only view of /proc."""

    def __init__(self, root='/proc'):
        self.root = Path(root)
        try:
            self.boot = time.time() - float((self.root / 'uptime').read_text().split()[0])
        except (OSError, ValueError, IndexError):
            self.boot = None

    def pids(self):
        return [int(p) for p in os.listdir(self.root) if p.isdigit()]

    def stat(self, pid):
        """(state, ppid, start time in epoch seconds), or None."""
        try:
            raw = (self.root / str(pid) / 'stat').read_text()
            fields = raw[raw.rfind(')') + 2:].split()
            return fields[0], int(fields[1]), self.boot + int(fields[19]) / _TICK
        except (OSError, IndexError, ValueError, TypeError):
            return None

    def thread(self, pid):
        try:
            env = (self.root / str(pid) / 'environ').read_bytes()
        except OSError:
            return None
        for item in env.split(b'\0'):
            if item.startswith(b'CODEX_THREAD_ID='):
                return item[16:].decode(errors='replace')
        return None

    def protected(self, pid):
        try:
            cmd = (self.root / str(pid) / 'cmdline').read_bytes()
        except OSError:
            return True
        return any(p in cmd for p in PROTECTED)


def stop_thread(thread_id, before, proc=None, kill=os.kill, grace=GRACE_SECONDS):
    """Stop processes of thread_id started at or before 'before', and their descendants."""
    proc = proc or Proc()
    me = os.getpid()
    info = {}
    for pid in proc.pids():
        st = proc.stat(pid) if pid != me else None
        if st and st[0] != 'Z':
            info[pid] = st
    targets = {pid for pid, (_, _, start) in info.items()
               if start <= before and proc.thread(pid) == thread_id and not proc.protected(pid)}
    grew = True
    while grew:  # sandboxed commands may run without the variable under a marked parent
        grew = False
        for pid, (_, ppid, _) in info.items():
            if ppid in targets and pid not in targets and not proc.protected(pid):
                targets.add(pid)
                grew = True
    pending = {}
    for pid in sorted(targets):
        with contextlib.suppress(OSError):
            kill(pid, signal.SIGTERM)
            pending[pid] = info[pid][2]

    def alive(pid, start):
        st = proc.stat(pid)
        return bool(st) and st[0] != 'Z' and st[2] == start
    deadline = time.monotonic() + grace
    while pending and time.monotonic() < deadline:
        pending = {p: s for p, s in pending.items() if alive(p, s)}
        if pending:
            time.sleep(0.1)
    for pid, start in pending.items():
        if alive(pid, start):  # same start time: not a reused PID
            with contextlib.suppress(OSError):
                kill(pid, signal.SIGKILL)
    return sorted(targets)


class InterruptWatcher:
    def __init__(self, root, models, proc=None, stop=stop_thread):
        self.sessions = Path(root) / 'sessions'
        self.db = Path(root) / 'state_5.sqlite'
        self.models = frozenset(models)
        self.proc = proc
        self.stop = stop
        self.files = {}
        self.primed = False

    def candidates(self):
        days = sorted(p for p in self.sessions.glob('*/*/*') if p.is_dir())[-2:]
        return [p for day in days for p in day.glob('*.jsonl')]

    def scan(self):
        """Process new rollout lines; return [(thread_id, stopped pids)]."""
        stopped, current = [], set()
        for path in self.candidates():
            current.add(path)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            state = self.files.get(path)
            if state is None:
                # History present at startup is never replayed.
                state = self.files[path] = {'offset': 0 if self.primed else size, 'thread': None, 'source': None, 'model': None}
            if size < state['offset']:
                state['offset'] = 0
            if size > state['offset']:
                stopped += self.follow(path, state, size)
        self.files = {p: s for p, s in self.files.items() if p in current}
        self.primed = True
        return stopped

    def follow(self, path, state, size):
        with path.open('rb') as stream:
            stream.seek(state['offset'])
            data = stream.read(min(size - state['offset'], MAX_CHUNK))
        end = data.rfind(b'\n')
        if end < 0:
            if len(data) >= MAX_CHUNK:
                state['offset'] += len(data)
            return []
        state['offset'] += end + 1
        stopped = []
        for line in data[:end].split(b'\n'):
            if b'session_meta' not in line and b'turn_context' not in line and b'turn_aborted' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            value = record.get('payload') if isinstance(record.get('payload'), dict) else {}
            kind = record.get('type')
            if kind == 'session_meta' and state['thread'] is None:
                state['thread'], state['source'] = value.get('id'), value.get('thread_source')
            elif kind == 'turn_context' and value.get('model'):
                state['model'] = value['model']
            elif kind == 'event_msg' and value.get('type') == 'turn_aborted' and value.get('reason') == 'interrupted':
                stopped += self.interrupted(path, state, record)
        return stopped

    def read_meta(self, path, state):
        with contextlib.suppress(OSError, ValueError):
            with path.open('rb') as stream:
                first = json.loads(stream.readline(MAX_CHUNK))
            value = first.get('payload') or {}
            if first.get('type') == 'session_meta':
                state['thread'], state['source'] = value.get('id'), value.get('thread_source')

    def lookup(self, thread_id):
        if not self.db.exists():
            return None, None
        try:
            connection = sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True, timeout=2)
            try:
                row = connection.execute('select thread_source, model from threads where id=?', (thread_id,)).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return None, None
        return row or (None, None)

    def interrupted(self, path, state, record):
        if state['thread'] is None:
            self.read_meta(path, state)
        thread_id, source, model = state['thread'], state['source'], state['model']
        if not isinstance(thread_id, str):
            return []
        if source is None or model is None:
            indexed = self.lookup(thread_id)
            source, model = source or indexed[0], model or indexed[1]
        before = epoch(record.get('timestamp'))
        if source != 'subagent' or model not in self.models or before is None:
            return []
        pids = self.stop(thread_id, before, self.proc)
        if pids:
            print(f'codex-relay: interrupted sub-agent {thread_id}: stopped {len(pids)} process(es)', file=sys.stderr, flush=True)
        return [(thread_id, pids)]

    async def run(self, interval=INTERVAL_SECONDS):
        while True:
            try:
                await asyncio.to_thread(self.scan)
            except Exception as exc:  # keep watching; never take the router down
                print('codex-relay: interrupt watcher error (' + type(exc).__name__ + ')', file=sys.stderr, flush=True)
            await asyncio.sleep(interval)

