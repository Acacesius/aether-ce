"""Parse CLI JSON lines (Claude ``stream-json``, Codex ``exec --json``) into run events (CLI_EXECUTOR.md).

``parse_line(line, kind)`` never raises: garbled input becomes a ``raw`` event, unknown JSON types produce nothing.
Event kinds: assistant_text, tool_use, tool_result, usage, final_result, error, raw. Every event is a plain dict with
``kind`` plus string/int/bool fields only.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading

from aether.studio import fsperm

TEXT_MAX = 4000
INPUT_MAX = 300
LINE_MAX = 1_000_000


def _text(value, limit=TEXT_MAX) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):   # content blocks: [{"type": "text", "text": ...}, ...]
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(_text(item.get('text', item.get('content', '')), limit))
            elif isinstance(item, str):
                parts.append(item)
        return '\n'.join(p for p in parts if p)[:limit]
    if isinstance(value, dict):
        return _text(value.get('text', value.get('message', '')), limit)
    return str(value)[:limit]


def _summary(value) -> str:
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= INPUT_MAX else text[:INPUT_MAX - 3] + '...'


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(raw, cost=None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    event = {'kind': 'usage', 'input_tokens': _int(raw.get('input_tokens')),
             'output_tokens': _int(raw.get('output_tokens')),
             'cached_tokens': _int(raw.get('cache_read_input_tokens')) + _int(raw.get('cached_input_tokens'))}
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        event['cost_usd'] = float(cost)
    return event


# ------------------------------------------------------------ Claude stream-json
def _claude(obj) -> list:
    kind = obj.get('type')
    message = obj.get('message') if isinstance(obj.get('message'), dict) else {}
    content = message.get('content')
    if kind == 'assistant':
        events = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'text' and block.get('text'):
                events.append({'kind': 'assistant_text', 'text': _text(block['text'])})
            elif block.get('type') == 'tool_use':
                events.append({'kind': 'tool_use', 'id': str(block.get('id', ''))[:100],
                               'name': str(block.get('name', ''))[:100], 'input': _summary(block.get('input'))})
        return events
    if kind == 'user':
        events = []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get('type') == 'tool_result':
                events.append({'kind': 'tool_result', 'id': str(block.get('tool_use_id', ''))[:100],
                               'text': _text(block.get('content')), 'is_error': bool(block.get('is_error'))})
        return events
    if kind == 'result':
        events = []
        usage = _usage(obj.get('usage'), obj.get('total_cost_usd'))
        if usage:
            events.append(usage)
        text = _text(obj.get('result')) or _text(obj.get('error')) or ', '.join(map(str, obj.get('errors') or []))[:TEXT_MAX]
        events.append({'kind': 'final_result', 'text': text, 'subtype': str(obj.get('subtype', ''))[:60],
                       'is_error': bool(obj.get('is_error')) or str(obj.get('subtype', 'success')) != 'success'})
        return events
    if kind == 'error':
        return [{'kind': 'error', 'text': _text(obj.get('error') or obj.get('message'))}]
    return []


# ------------------------------------------------------------ Codex exec --json
def _codex_item(item, phase) -> list:
    itype = item.get('type') or item.get('item_type')
    iid = str(item.get('id', ''))[:100]
    if itype in ('agent_message', 'assistant_message') and phase == 'completed':
        return [{'kind': 'assistant_text', 'text': _text(item.get('text'))}]
    if itype == 'command_execution':
        if phase == 'started':
            return [{'kind': 'tool_use', 'id': iid, 'name': 'command', 'input': _summary(item.get('command'))}]
        if phase == 'completed':
            code = item.get('exit_code')
            return [{'kind': 'tool_result', 'id': iid, 'text': _text(item.get('aggregated_output')),
                     'is_error': item.get('status') == 'failed' or (isinstance(code, int) and code != 0)}]
    if itype == 'file_change' and phase == 'completed':
        return [{'kind': 'tool_use', 'id': iid, 'name': 'file_change', 'input': _summary(item.get('changes'))}]
    if itype in ('mcp_tool_call', 'web_search') and phase in ('started', 'completed'):
        name = itype if itype == 'web_search' else f'{item.get("server", "")}.{item.get("tool", "")}'
        return [{'kind': 'tool_use', 'id': iid, 'name': name[:100],
                 'input': _summary(item.get('arguments', item.get('query')))}] if phase == 'started' else []
    if itype == 'error':
        return [{'kind': 'error', 'text': _text(item.get('message'))}]
    return []


def _codex_legacy(msg) -> list:
    mtype = msg.get('type')
    if mtype == 'agent_message':
        return [{'kind': 'assistant_text', 'text': _text(msg.get('message'))}]
    if mtype == 'exec_command_begin':
        return [{'kind': 'tool_use', 'id': str(msg.get('call_id', ''))[:100], 'name': 'command',
                 'input': _summary(msg.get('command'))}]
    if mtype == 'exec_command_end':
        return [{'kind': 'tool_result', 'id': str(msg.get('call_id', ''))[:100],
                 'text': _text(msg.get('aggregated_output') or msg.get('stdout') or msg.get('stderr')),
                 'is_error': _int(msg.get('exit_code')) != 0}]
    if mtype == 'token_count':
        info = msg.get('info') if isinstance(msg.get('info'), dict) else msg
        usage = _usage(info.get('last_token_usage') or info.get('total_token_usage') or info)
        return [usage] if usage and (usage['input_tokens'] or usage['output_tokens']) else []
    if mtype == 'task_complete':
        return [{'kind': 'final_result', 'text': _text(msg.get('last_agent_message')), 'subtype': 'success',
                 'is_error': False}]
    if mtype in ('error', 'stream_error'):
        return [{'kind': 'error', 'text': _text(msg.get('message'))}]
    return []


def _codex(obj) -> list:
    if isinstance(obj.get('msg'), dict):
        return _codex_legacy(obj['msg'])
    kind = str(obj.get('type', ''))
    if kind.startswith('item.') and isinstance(obj.get('item'), dict):
        return _codex_item(obj['item'], kind[5:])
    if kind == 'turn.completed':
        usage = _usage(obj.get('usage'))
        return [usage] if usage else []
    if kind == 'turn.failed':
        error = obj.get('error')
        return [{'kind': 'error', 'text': _text(error if not isinstance(error, dict) else error.get('message'))}]
    if kind == 'error':
        return [{'kind': 'error', 'text': _text(obj.get('message') or obj.get('error'))}]
    return []


def parse_line(line, kind: str) -> list:
    """Events for one stdout line of a ``kind`` ('claude' | 'codex') CLI. Never raises."""
    if isinstance(line, bytes):
        line = line.decode('utf-8', 'replace')
    line = str(line).strip()
    if not line:
        return []
    if len(line) > LINE_MAX:
        return [{'kind': 'raw', 'text': line[:TEXT_MAX]}]
    try:
        obj = json.loads(line)
    except ValueError:
        return [{'kind': 'raw', 'text': line[:TEXT_MAX]}]
    if not isinstance(obj, dict):
        return [{'kind': 'raw', 'text': line[:TEXT_MAX]}]
    try:
        return _claude(obj) if kind == 'claude' else _codex(obj)
    except Exception:   # malformed-but-JSON shapes must never break a run
        return [{'kind': 'raw', 'text': line[:TEXT_MAX]}]


def render(event) -> str:
    """One-line human text for a ``stage.log`` event."""
    kind = event['kind']
    if kind == 'tool_use':
        return f'tool_use {event.get("name", "")} {event.get("input", "")}'.rstrip()
    if kind == 'tool_result':
        return ('tool_result (error) ' if event.get('is_error') else 'tool_result ') + event.get('text', '')
    if kind == 'usage':
        return f'usage in={event["input_tokens"]} out={event["output_tokens"]} cached={event["cached_tokens"]}'
    if kind == 'final_result':
        return ('final_result (error) ' if event.get('is_error') else 'final_result ') + event.get('text', '')
    return event.get('text', '')


# ------------------------------------------------------------ raw log
HEADER = '[aether] '
STDERR = '[stderr] '


class RawLog:
    """Append-only UTF-8 log of everything a CLI printed for one run (one file per run, one section per attempt)."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def header(self, **fields) -> None:
        fields.setdefault('utc', datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
        self._write(HEADER + ' '.join(f'{k}={v}' for k, v in fields.items()))

    def stdout(self, line: str) -> None:
        self._write(line)

    def stderr(self, line: str) -> None:
        self._write(STDERR + line)

    def _write(self, line: str) -> None:
        line = line.rstrip('\r\n').replace('\r', ' ').replace('\n', ' ')
        with self._lock:
            fresh = not self.path.exists()
            with open(self.path, 'a', encoding='utf-8', errors='replace', newline='\n') as stream:
                if fresh:       # owner-only from the first byte, on top of the owner-only folder
                    fsperm.restrict(self.path, directory=False)
                stream.write(line + '\n')

    def tail(self, max_bytes: int = 65536) -> list:
        """Lines of the last attempt section (after the last header), from at most ``max_bytes`` of the file end."""
        try:
            with open(self.path, 'rb') as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - max_bytes))
                data = stream.read()
        except FileNotFoundError:
            return []
        lines = data.decode('utf-8', 'replace').splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]   # first line is probably cut
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith(HEADER):
                return lines[i + 1:]
        return lines
