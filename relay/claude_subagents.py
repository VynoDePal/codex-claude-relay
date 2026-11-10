"""Recover plain-text messages between Codex agents for Claude sub-agents.

Codex delivers spawn_agent, followup_task and send_message payloads to the
target agent as an agent_message whose body is OpenAI-encrypted; only OpenAI
servers can read it. When the sender ran on Claude, its own local rollout still
holds the call in plain text. Each encrypted message is matched to exactly one
such call, using the delivery link Codex records (SubAgentActivity), the message
type and the time order. Anything incomplete or ambiguous is an error: a
sub-agent never receives a guessed task.
"""
import copy
import json
import sqlite3
from datetime import datetime

from claude_history import MAX_LINE_BYTES, UUID, RolloutReader
from gpu_vision import VisionError

# Message type each collaboration call produces at the recipient.
CALL_TYPES = {'spawn_agent': 'NEW_TASK', 'followup_task': 'NEW_TASK', 'send_message': 'MESSAGE'}
ACTIVITY_KINDS = ('started', 'interacted')
MAX_SCAN_BYTES = 4 * 1024 * 1024 * 1024
MAX_PAGES = 64
CACHE_SIZE = 4096
FERNET_PREFIX = 'gAAAAA'
_recovered = {}

ALTERNATIVES = ('Relaunch this sub-agent with an OpenAI model (spawn_agent with model, for example gpt-6.1-sol), '
                'or delegate to Claude through the claude_code connector.')


def failed(detail):
    return VisionError('Claude could not recover a message between Codex agents from local history: ' + detail
                       + '. No task was guessed. ' + ALTERNATIVES, 400)


def openai_sender(author):
    return VisionError('Claude cannot read this message from ' + author + ': that agent wrote it with an OpenAI model, '
                       'and Codex keeps such messages only in encrypted form. ' + ALTERNATIVES, 400)


def is_encrypted(item):
    return (isinstance(item, dict) and item.get('type') == 'agent_message'
            and any(isinstance(p, dict) and p.get('type') == 'encrypted_content' for p in item.get('content') or []))


def readable_parts(item):
    return [p for p in item.get('content') or [] if isinstance(p, dict) and p.get('type') != 'encrypted_content']


def message_type(item):
    for part in readable_parts(item):
        for line in str(part.get('text', '')).splitlines():
            if line.startswith('Message Type: '):
                return line[len('Message Type: '):].strip()
    return None


def delivered_ms(item):
    """agent_message IDs are amsg_<UUIDv7>; the first 48 bits are the delivery time in ms."""
    ident = item.get('id')
    digits = ident[5:].replace('-', '') if isinstance(ident, str) and ident.startswith('amsg_') else ''
    try:
        if len(digits) != 32:
            raise ValueError
        return int(digits[:12], 16)
    except ValueError:
        raise failed('a message has no time-ordered identifier') from None


def record_ms(record):
    try:
        return round(datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp() * 1000)
    except (KeyError, AttributeError, TypeError, ValueError):
        return None


def first_record(reader, path):
    with reader.open_page(path) as stream:
        raw = stream.readline(MAX_LINE_BYTES + 1)
    record = json.loads(raw) if raw else None
    if not isinstance(record, dict) or record.get('type') != 'session_meta':
        raise failed('a history page has no session metadata')
    return record.get('payload') or {}


class Agents:
    """Resolve agent paths (/root, /root/worker) to thread IDs within one Codex session."""

    def __init__(self, reader, thread_id):
        self.reader = reader
        self.session = first_record(reader, reader.thread(thread_id)).get('session_id') or thread_id
        if not isinstance(self.session, str) or not UUID.fullmatch(self.session):
            raise failed('the thread has an invalid session identifier')
        self.known = {'/root': self.session}

    def resolve(self, path):
        if path in self.known:
            return self.known[path]
        if not isinstance(path, str) or not path.startswith('/root/'):
            raise failed('a message names an invalid agent')
        db = self.reader.root / 'state_5.sqlite'
        connection = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            rows = connection.execute('select id, rollout_path from threads where agent_path=?', (path,)).fetchall()
        finally:
            connection.close()
        found = []
        for ident, rollout in rows:
            if not (isinstance(ident, str) and UUID.fullmatch(ident) and rollout):
                continue
            try:
                same = first_record(self.reader, self.reader.checked_path(rollout)).get('session_id') == self.session
            except (OSError, ValueError, VisionError):
                continue
            if same:
                found.append(ident)
        if len(found) != 1:
            raise failed('the agent ' + path + ' is missing or ambiguous in the local thread index')
        self.known[path] = found[0]
        return found[0]


def scan(reader, path, enough):
    """Collect collaboration calls and delivery links, newest page first, until enough is found."""
    calls, links, seen, end, total = [], {}, set(), None, 0
    while True:
        path = reader.checked_path(path)
        if path in seen or len(seen) >= MAX_PAGES:
            raise failed('cyclic or excessively deep history references')
        seen.add(path)
        page, base, first = [], None, True
        with reader.open_page(path) as stream:
            while end is None or stream.tell() < end:
                raw = stream.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                total += len(raw)
                if len(raw) > MAX_LINE_BYTES or total > MAX_SCAN_BYTES:
                    raise failed('history exceeds the scan size limit')
                if first:
                    first = False
                    record = json.loads(raw)
                    if not isinstance(record, dict) or record.get('type') != 'session_meta':
                        raise failed('a history page has no session metadata')
                    base = (record.get('payload') or {}).get('history_base')
                    continue
                if b'collaboration' not in raw and b'SubAgentActivity' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue  # torn line: Codex rewrites the full record on the next line
                if not isinstance(record, dict):
                    continue
                value = record.get('payload') if isinstance(record.get('payload'), dict) else {}
                if (record.get('type') == 'response_item' and value.get('type') == 'function_call'
                        and value.get('namespace') == 'collaboration' and value.get('name') in CALL_TYPES):
                    try:
                        message = json.loads(value.get('arguments') or '{}').get('message')
                    except (ValueError, AttributeError):
                        message = None
                    page.append({'call_id': value.get('call_id'), 'name': value['name'],
                                 'message': message, 'ms': record_ms(record)})
                elif record.get('type') == 'event_msg' and value.get('type') == 'item_completed':
                    item = value.get('item') or {}
                    if item.get('type') == 'SubAgentActivity' and item.get('kind') in ACTIVITY_KINDS and item.get('id'):
                        links[item['id']] = item.get('agent_thread_id')
        calls = page + calls
        if enough(calls, links) or not base:
            return calls, links
        offset = base.get('end_byte_offset')
        if not isinstance(offset, int) or offset < 0:
            raise failed('invalid history reference limits')
        path, end = reader.page(base.get('thread_id')), offset


def match(reader, thread_id, todo):
    agents = Agents(reader, thread_id)
    groups = {}
    for index, item, _ in todo:
        groups.setdefault((item.get('author'), item.get('recipient')), []).append((index, item))
    result = {}
    for (author, recipient), entries in groups.items():
        sender, target = agents.resolve(author), agents.resolve(recipient)
        times = [delivered_ms(item) for _, item in entries]
        if times != sorted(times):
            raise failed('messages from ' + str(author) + ' are out of order')
        last, count = times[-1], len(entries)

        def relevant(calls, links, target=target, last=last):
            # Mailboxes are FIFO: messages delivered so far are the calls made before the last delivery.
            return [c for c in calls if links.get(c['call_id']) == target and c['ms'] is not None and c['ms'] <= last]

        calls, links = scan(reader, reader.thread(sender), lambda c, l: len(relevant(c, l)) >= count)
        candidates = sorted(relevant(calls, links), key=lambda c: c['ms'])
        if len(candidates) < count:
            raise failed(f'{author} delivered {count} message(s) to {recipient}, '
                         f'but its local history holds only {len(candidates)} matching call(s)')
        # A compacted conversation keeps only its latest messages: match the most recent calls.
        for (index, item), call, ms in zip(entries, candidates[-count:], times):
            if call['ms'] > ms or CALL_TYPES[call['name']] != message_type(item):
                raise failed('a message from ' + author + ' does not match its ' + call['name'] + ' call')
            text = call['message']
            if not isinstance(text, str) or not text.strip():
                raise failed('the matching ' + call['name'] + ' call has no message text')
            if text.startswith(FERNET_PREFIX):
                raise openai_sender(author)
            rebuilt = copy.deepcopy(item)
            rebuilt['content'] = readable_parts(item) + [{'type': 'input_text', 'text': text}]
            result[index] = rebuilt
    return result


def recover_agent_messages(payload, root, headers):
    items = payload.get('input')
    if not isinstance(items, list) or not any(is_encrypted(i) for i in items):
        return payload
    restored, todo = {}, []
    for index, item in enumerate(items):
        if is_encrypted(item):
            key = (item.get('id'), item.get('author'), item.get('recipient'))
            if key in _recovered:
                restored[index] = copy.deepcopy(_recovered[key])
            else:
                todo.append((index, item, key))
    if todo:
        thread_id = headers.get('thread-id')
        if not isinstance(thread_id, str) or not UUID.fullmatch(thread_id):
            raise failed('the request does not identify its Codex thread')
        try:
            found = match(RolloutReader(root), thread_id, todo)
        except VisionError:
            raise
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            raise failed('local history is unavailable or malformed (' + type(exc).__name__ + ')') from None
        for index, _, key in todo:
            restored[index] = found[index]
            if len(_recovered) >= CACHE_SIZE:
                _recovered.pop(next(iter(_recovered)))
            _recovered[key] = copy.deepcopy(found[index])
    work = dict(payload)
    work['input'] = [restored.get(i, item) for i, item in enumerate(items)]
    return work

