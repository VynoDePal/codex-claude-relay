"""Readable messages between Codex agents.

Codex delivers spawn_agent, followup_task and send_message payloads to the
recipient as an agent_message whose body sits in an encrypted_content part.
What that field holds depends on the model that wrote the call:

- OpenAI models return call arguments already encrypted, so the field holds an
  OpenAI token (Fernet, "gAAAAA..."). Only OpenAI servers can read it; no
  readable copy exists locally, not even in the sender's own history.
- Claude and other relayed models return plain arguments, and Codex stores the
  message text itself in that field: it is the sender's message as written.

unwrap_plain turns the second kind into ordinary input_text, so Claude can read
it and OpenAI does not reject it as unverifiable encrypted content.
"""
import re

from gpu_vision import VisionError

FERNET = re.compile(r'gAAAAA[A-Za-z0-9_-]{60,}={0,2}\Z')
ALTERNATIVES = ('Relaunch this sub-agent with an OpenAI model (spawn_agent with model, for example gpt-6.1-sol), '
                'or delegate to Claude through the claude_code connector.')


def is_token(value):
    return isinstance(value, str) and FERNET.match(value) is not None


def unwrap_plain(payload):
    """Return (payload, changed); plain encrypted_content parts of agent messages become input_text."""
    items = payload.get('input')
    if not isinstance(items, list):
        return payload, False
    changed, out = False, []
    for item in items:
        if isinstance(item, dict) and item.get('type') == 'agent_message' and isinstance(item.get('content'), list):
            parts, touched = [], False
            for part in item['content']:
                body = part.get('encrypted_content') if isinstance(part, dict) else None
                if isinstance(part, dict) and part.get('type') == 'encrypted_content' and isinstance(body, str) and not is_token(body):
                    parts.append({'type': 'input_text', 'text': body})
                    touched = True
                else:
                    parts.append(part)
            if touched:
                item, changed = {**item, 'content': parts}, True
        out.append(item)
    return ({**payload, 'input': out}, True) if changed else (payload, False)


def recover_agent_messages(payload):
    """Make agent messages readable for Claude, or explain why one cannot be."""
    payload, _ = unwrap_plain(payload)
    items = payload.get('input')
    for item in items if isinstance(items, list) else []:
        if (isinstance(item, dict) and item.get('type') == 'agent_message'
                and any(isinstance(p, dict) and p.get('type') == 'encrypted_content' for p in item.get('content') or [])):
            raise VisionError('Claude cannot read this message from ' + str(item.get('author') or 'another agent')
                              + ': that agent wrote it with an OpenAI model, and Codex keeps such messages only in '
                              'OpenAI-encrypted form. ' + ALTERNATIVES, 400)
    return payload

