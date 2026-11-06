"""Compaction of the Codex code-mode tool catalog, shared by the Claude and Gemini relays.

In code mode the exec tool description lists every nested tool with its full
TypeScript declaration: about 780,000 characters with the user's plugins on
2026-10-30. Each nested tool's full text stays reachable at run time through
ALL_TOOLS inside exec, so a relay may abbreviate sections and point the model there.
"""
import collections
import re

ABBREVIATED_LINE_CHARS = 240
SLIM_LINE_CHARS = 200
_HEADING = re.compile(r'^(##|###) ')
_TOOL_HEADING = re.compile(r'^### \x60([^\x60]+)\x60')
_PLUGIN_SUFFIX = re.compile(r'\s*This tool is part of plugin \x60[^\x60]*\x60\.?\s*$')
# MCP servers whose nested tools stay verbatim: Codex app management and the
# local delegation connectors are used routinely. App connectors and
# platform-specific servers (iOS) are abbreviated and read on demand.
FULL_SERVERS = frozenset({'codex_app', 'claude_code', 'gpu_agents', 'node_repl'})
SLIM_NOTE = ('\n\nNote from the model transport: {count} nested tool and namespace descriptions above are '
             'abbreviated (marked [abbreviated]) to keep each request small. Before calling an abbreviated '
             'tool, read its full description and argument declaration inside exec, for example '
             "text(ALL_TOOLS.find(t => t.name === 'TOOL_NAME').description); that text also includes its "
             'namespace description. Then call it as tools.TOOL_NAME(...).\n')


def _sections(description):
    """Split a code-mode catalog into (kind, name, text) units.

    Units start at a tool heading (### \x60name\x60) or at a namespace heading
    (## name) that prefixes at least one tool. Other markdown headings inside a
    description stay part of the current unit.
    """
    lines = description.split('\n')
    names = [m.group(1) for line in lines for m in [_TOOL_HEADING.match(line)] if m]
    units, kind, name, current = [], 'preamble', None, []
    for line in lines:
        tool = _TOOL_HEADING.match(line)
        space = None
        if not tool and line.startswith('## ') and ' ' not in line[3:].strip():
            candidate = line[3:].strip()
            if any(n.startswith(candidate + '_') for n in names):
                space = candidate
        if tool or space:
            units.append([kind, name, '\n'.join(current)])
            kind, name = ('tool', tool.group(1)) if tool else ('namespace', space)
            current = [line]
        else:
            current.append(line)
    units.append([kind, name, '\n'.join(current)])
    return units


def _first_lines(text):
    return [line.strip() for line in text.partition('\n')[2].split('\n') if line.strip()]


def _abbreviate(text, shared_lines=frozenset(), limit=ABBREVIATED_LINE_CHARS, strip_plugin=False):
    heading, _, body = text.partition('\n')
    # Code-mode repeats the app description at the top of every tool in that
    # app; keep the first line that describes this particular tool instead.
    lines = _first_lines(text)
    first = next((line for line in lines if line not in shared_lines), lines[0] if lines else '')
    if strip_plugin:
        first = _PLUGIN_SUFFIX.sub('', first)
    if len(first) > limit:
        first = first[:limit - 3].rstrip() + '...'
    return heading + '\n' + first + ' [abbreviated]\n'


def _server(name):
    parts = (name or '').split('__')
    return parts[1] if len(parts) > 1 and parts[0] == 'mcp' else None


def slim_catalog(chat, full_servers=FULL_SERVERS, limit=SLIM_LINE_CHARS):
    """Abbreviate nested MCP tools outside full_servers. Deterministic, so the
    catalog block stays an identical cached prefix from one request to the next."""
    count = 0
    for entry in chat.get('tools') or []:
        fn = entry.get('function') or {}
        description = fn.get('description') or ''
        if not any(_TOOL_HEADING.match(line) for line in description.split('\n')):
            continue
        units = _sections(description)
        firsts = collections.Counter()
        for unit in units:
            if unit[0] == 'tool':
                for line in _first_lines(unit[2])[:2]:
                    firsts[line] += 1
        shared = frozenset(line for line, n in firsts.items() if n > 1)
        changed = 0
        for unit in units:
            server = _server(unit[1]) if unit[0] != 'preamble' else None
            if server is None or server in full_servers:
                continue
            short = _abbreviate(unit[2], shared, limit, strip_plugin=True)
            if len(short) < len(unit[2]):
                unit[2] = short
                changed += 1
        if changed:
            fn['description'] = '\n'.join(u[2] for u in units).rstrip('\n') + SLIM_NOTE.format(count=changed)
            count += changed
    return count

